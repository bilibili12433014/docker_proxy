from __future__ import annotations

import base64
import hmac
import logging
import re
import secrets
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

from docker.errors import DockerException
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .config import ADMIN_PASSWORD_RE, HOSTNAME_RE, Settings
from .config_store import ConfigStore, UserRecord
from .docker_service import DockerService
from .keys import KeyManager

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
GPU_ID_RE = re.compile(r"^(?:0|[1-9][0-9]*|GPU-[A-Fa-f0-9-]+)$")
F = TypeVar("F", bound=Callable[..., Any])
logger = logging.getLogger(__name__)


def powershell_key_command(private_key: bytes, operation: str) -> str:
    encoded_key = base64.b64encode(private_key).decode("ascii")
    return "".join(
        [
            "$key=Join-Path $env:TEMP ('docker-proxy-'+[guid]::NewGuid().ToString('N')+'.key');",
            "try{",
            f"[IO.File]::WriteAllBytes($key,[Convert]::FromBase64String('{encoded_key}'));",
            "$sid=([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value;",
            "icacls $key /inheritance:r | Out-Null;",
            "icacls $key /grant:r (\"*$($sid):R\") | Out-Null;",
            "if(-not(Get-Command cloudflared -ErrorAction SilentlyContinue)){",
            "winget install --id Cloudflare.cloudflared -e --source winget --accept-source-agreements --accept-package-agreements | Out-Null};",
            operation,
            "}finally{Remove-Item -LiteralPath $key -Force -ErrorAction SilentlyContinue}",
        ]
    )


def powershell_command(username: str, hostname: str, private_key: bytes) -> str:
    return powershell_key_command(
        private_key,
        f"ssh -i $key -o IdentitiesOnly=yes -o \"ProxyCommand=cloudflared access ssh --hostname %h\" {username}@{hostname};",
    )


def powershell_scp_command(username: str, hostname: str, private_key: bytes) -> str:
    return '$file="FILE_NAME";' + powershell_key_command(
        private_key,
        f"scp -O -i $key -o IdentitiesOnly=yes -o \"ProxyCommand=cloudflared access ssh --hostname %h\" \"$file\" \"{username}@{hostname}:/data/\";",
    )


def create_app(
    settings: Settings,
    store: ConfigStore,
    docker_service: DockerService,
    key_manager: KeyManager,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=settings.secure_cookies,
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    create_lock = threading.Lock()

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    def indexed_gpu_ids(values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or "all" in values:
            return values
        devices, _ = docker_service.gpu_inventory()
        indexes = {device.device_id: str(device.index) for device in devices}
        return tuple(dict.fromkeys(indexes.get(value, value) for value in values))

    def selected_gpu_ids() -> tuple[str, ...]:
        values = tuple(dict.fromkeys(request.form.getlist("gpu_ids")))
        if not values:
            return ()
        if "all" in values:
            if len(values) != 1:
                raise ValueError("全部 GPU 不能与单独显卡同时选择")
            devices, _ = docker_service.gpu_inventory()
            return tuple(str(device.index) for device in devices) or ("all",)
        if not all(GPU_ID_RE.fullmatch(value) for value in values):
            raise ValueError("GPU 编号格式无效")
        devices, _ = docker_service.gpu_inventory()
        available = {
            value
            for device in devices
            for value in (str(device.index), device.device_id)
        }
        if available and not set(values).issubset(available):
            raise ValueError("选择中包含当前不可用的 GPU")
        indexes = {device.device_id: str(device.index) for device in devices}
        return tuple(dict.fromkeys(indexes.get(value, value) for value in values))

    def login_required(view: F) -> F:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not session.get("authenticated"):
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return cast(F, wrapped)

    @app.before_request
    def require_initialization() -> Any:
        if not store.is_initialized() and request.endpoint not in {
            "setup",
            "static",
            "healthz",
        }:
            return redirect(url_for("setup"))
        return None

    @app.before_request
    def validate_csrf() -> None:
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            provided = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected, provided):
                abort(400, "CSRF 校验失败")

    @app.after_request
    def security_headers(response: Any) -> Any:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        return response

    @app.route("/setup", methods=["GET", "POST"])
    def setup() -> Any:
        if store.is_initialized():
            return redirect(url_for("dashboard" if session.get("authenticated") else "login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            confirmation = request.form.get("confirmation", "")
            hostname = request.form.get("hostname", "").strip()
            if not ADMIN_PASSWORD_RE.fullmatch(password):
                flash("管理密码只能包含英文字母、数字和常用 ASCII 符号", "error")
            elif password != confirmation:
                flash("两次输入的管理密码不一致", "error")
            elif not HOSTNAME_RE.fullmatch(hostname):
                flash("SSH 公网域名格式无效", "error")
            else:
                secret_key = secrets.token_urlsafe(48)
                initialized = store.initialize(
                    settings.initial_config(password, hostname, secret_key)
                )
                if initialized:
                    session.clear()
                    session["authenticated"] = True
                    session["csrf_token"] = secrets.token_urlsafe(32)
                    return redirect(url_for("dashboard"))
                return redirect(url_for("login"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if request.method == "POST":
            supplied = request.form.get("password", "")
            if hmac.compare_digest(supplied, store.admin_password()):
                session.clear()
                session["authenticated"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("dashboard"))
            flash("管理密码错误", "error")
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard() -> Any:
        hostname = store.public_hostname()
        docker_ok, docker_message = docker_service.ping()
        gpu_devices, gpu_error = (
            docker_service.gpu_inventory()
            if docker_ok
            else ((), "Docker 离线，无法检测 GPU")
        )
        try:
            host_info = docker_service.host_info() if docker_ok else None
        except DockerException as exc:
            logger.warning("读取 Docker 主机信息失败: %s", exc)
            host_info = None
        users = []
        for user in store.list_users():
            private_path = key_manager.private_key_path(user.username)
            private_key = private_path.read_bytes() if private_path.is_file() else None
            users.append(
                {
                    "record": user,
                    "status": docker_service.status(user.container_id),
                    "gpu_ids": indexed_gpu_ids(user.gpu_ids),
                    "powershell": (
                        powershell_command(user.username, hostname, private_key)
                        if private_key is not None
                        else ""
                    ),
                    "scp": (
                        powershell_scp_command(user.username, hostname, private_key)
                        if private_key is not None
                        else ""
                    ),
                }
            )
        return render_template(
            "dashboard.html",
            users=users,
            hostname=hostname,
            default_image=store.last_image(),
            docker_ok=docker_ok,
            docker_message=docker_message,
            gpu_devices=gpu_devices,
            gpu_error=gpu_error,
            host_info=host_info,
        )

    @app.post("/users")
    @login_required
    def create_user() -> Any:
        username = request.form.get("username", "").strip()
        image = request.form.get("image", "").strip()
        logger.info(
            "收到创建用户请求 username=%s image=%s reuse_existing=%s reset_existing=%s",
            username,
            image,
            request.form.get("reuse_existing"),
            request.form.get("reset_existing"),
        )
        if not USERNAME_RE.fullmatch(username):
            logger.warning("创建用户校验失败 username=%s 原因=用户名格式无效", username)
            flash(
                "用户名必须以小写字母或下划线开头，且只能包含小写字母、数字、下划线和连字符，最长 32 位",
                "error",
            )
            return redirect(url_for("dashboard"))
        if not IMAGE_RE.fullmatch(image):
            logger.warning(
                "创建用户校验失败 username=%s image=%s 原因=镜像格式无效",
                username,
                image,
            )
            flash("Docker 镜像名称格式无效", "error")
            return redirect(url_for("dashboard"))
        with create_lock:
            stored_user = store.get_user(username)
            try:
                existing = docker_service.existing_instance(username)
            except DockerException as exc:
                logger.exception("检查同名容器失败 username=%s", username)
                flash(f"检查同名容器失败: {exc}", "error")
                return redirect(url_for("dashboard"))
            if existing is not None:
                if not existing.managed:
                    logger.error(
                        "拒绝操作非本项目容器 username=%s container=%s id=%s",
                        username,
                        existing.name,
                        existing.container_id,
                    )
                    flash(
                        f"同名容器 {existing.name} 不是本项目创建的，请手动重命名或删除",
                        "error",
                    )
                    return redirect(url_for("dashboard"))
                if request.form.get("reuse_existing") == "yes":
                    phase = "重置 SSH 密钥"
                    try:
                        logger.debug(
                            "复用容器阶段 username=%s phase=%s container_id=%s",
                            username,
                            phase,
                            existing.container_id,
                        )
                        user_key = key_manager.reset_user_key(username)
                        phase = "保存用户配置"
                        store.reuse_user(
                            username,
                            existing.image,
                            existing.container_id,
                            user_key.public_key,
                            indexed_gpu_ids(existing.gpu_ids),
                        )
                    except (OSError, TypeError, ValueError) as exc:
                        logger.exception(
                            "复用实例失败 username=%s container_id=%s phase=%s",
                            username,
                            existing.container_id,
                            phase,
                        )
                        flash(f"复用实例失败: {exc}", "error")
                        return redirect(url_for("dashboard"))
                    logger.warning(
                        "已复用容器并重置 SSH 密钥 username=%s container_id=%s config_existed=%s gpu_ids=%s",
                        username,
                        existing.container_id,
                        stored_user is not None,
                        indexed_gpu_ids(existing.gpu_ids),
                    )
                    flash(
                        f"已复用 {username} 的容器并重置 SSH 密钥，旧密钥立即失效",
                        "success",
                    )
                    return redirect(url_for("dashboard"))
                try:
                    gpu_ids = selected_gpu_ids()
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("dashboard"))
                if request.form.get("reset_existing") != "yes":
                    logger.info(
                        "等待用户选择复用或重建 username=%s container_id=%s config_existed=%s",
                        username,
                        existing.container_id,
                        stored_user is not None,
                    )
                    store.set_last_image(image)
                    return render_template(
                        "confirm_reset.html",
                        username=username,
                        image=image,
                        instance=existing,
                        existing_gpu_ids=indexed_gpu_ids(existing.gpu_ids),
                        gpu_ids=gpu_ids,
                    )
            elif stored_user is not None:
                logger.error(
                    "用户配置存在但同名容器缺失 username=%s configured_container=%s",
                    username,
                    stored_user.container_id,
                )
                flash("用户配置存在，但同名受管容器已经缺失，无法复用", "error")
                return redirect(url_for("dashboard"))
            if existing is None:
                try:
                    gpu_ids = selected_gpu_ids()
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("dashboard"))
            user_key = None
            container_id = None
            phase = "准备创建"
            try:
                if existing is not None:
                    phase = "删除旧容器"
                    logger.warning(
                        "用户确认重建 username=%s container_id=%s",
                        username,
                        existing.container_id,
                    )
                    docker_service.remove_instance(existing.container_id)
                phase = "清理残留 SSH 密钥"
                logger.debug("创建用户阶段 username=%s phase=%s", username, phase)
                if key_manager.remove_user_key(username):
                    logger.warning("已清理孤立密钥目录 username=%s", username)
                phase = "保存镜像配置"
                logger.debug("创建用户阶段 username=%s phase=%s", username, phase)
                store.set_last_image(image)
                phase = "生成 SSH 密钥"
                logger.debug("创建用户阶段 username=%s phase=%s", username, phase)
                user_key = key_manager.create_user_key(username)
                phase = "创建 Docker 容器"
                logger.debug("创建用户阶段 username=%s phase=%s", username, phase)
                container_id = docker_service.create_instance(
                    username, image, gpu_ids
                )
                phase = "保存用户配置"
                logger.debug("创建用户阶段 username=%s phase=%s", username, phase)
                if stored_user is None:
                    store.add_user(
                        username,
                        image,
                        container_id,
                        user_key.public_key,
                        gpu_ids,
                    )
                else:
                    store.reuse_user(
                        username,
                        image,
                        container_id,
                        user_key.public_key,
                        gpu_ids,
                    )
            except (DockerException, OSError, TypeError, ValueError) as exc:
                logger.exception(
                    "创建用户失败 username=%s image=%s phase=%s",
                    username,
                    image,
                    phase,
                )
                if container_id:
                    try:
                        docker_service.remove_instance(container_id)
                    except DockerException:
                        logger.exception(
                            "回滚容器失败 username=%s container_id=%s",
                            username,
                            container_id,
                        )
                try:
                    if key_manager.remove_user_key(username):
                        logger.info("已回滚 SSH 密钥 username=%s", username)
                except (OSError, ValueError):
                    logger.exception("回滚 SSH 密钥失败 username=%s", username)
                flash(f"创建用户失败: {exc}", "error")
                return redirect(url_for("dashboard"))
        logger.info(
            "创建用户完成 username=%s image=%s container_id=%s gpu_ids=%s",
            username,
            image,
            container_id,
            gpu_ids,
        )
        flash(f"用户 {username} 已创建，容器处于停止状态", "success")
        return redirect(url_for("dashboard"))

    @app.route("/users/<username>/gpu", methods=["GET", "POST"])
    @login_required
    def configure_gpu(username: str) -> Any:
        user = require_user(store, username)
        try:
            gpu_devices, gpu_error = docker_service.gpu_inventory()
        except DockerException as exc:
            gpu_devices, gpu_error = (), str(exc)
        if request.method == "GET":
            return render_template(
                "configure_gpu.html",
                user=user,
                gpu_devices=gpu_devices,
                gpu_error=gpu_error,
                current_gpu_ids=indexed_gpu_ids(user.gpu_ids),
            )
        try:
            gpu_ids = selected_gpu_ids()
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("configure_gpu", username=username))
        with create_lock:
            user = require_user(store, username)
            try:
                existing = docker_service.existing_instance(username)
                if (
                    existing is None
                    or not existing.managed
                    or existing.container_id != user.container_id
                ):
                    flash("用户配置与同名受管容器不匹配，无法修改 GPU", "error")
                    return redirect(url_for("dashboard"))
                existing_gpu_ids = indexed_gpu_ids(existing.gpu_ids)
                if existing_gpu_ids == gpu_ids:
                    if user.gpu_ids != gpu_ids:
                        store.update_instance(username, existing.container_id, gpu_ids)
                    flash(f"{username} 的 GPU 分配没有变化", "success")
                    return redirect(url_for("dashboard"))
                logger.warning(
                    "开始修改 GPU，容器将被重建 username=%s old_gpu_ids=%s new_gpu_ids=%s",
                    username,
                    existing_gpu_ids,
                    gpu_ids,
                )
                container_id = docker_service.recreate_instance(
                    username,
                    user.image,
                    user.container_id,
                    gpu_ids,
                )
                store.update_instance(username, container_id, gpu_ids)
            except (DockerException, OSError, TypeError, ValueError) as exc:
                logger.exception("修改 GPU 失败 username=%s gpu_ids=%s", username, gpu_ids)
                flash(f"修改 GPU 失败: {exc}", "error")
                return redirect(url_for("configure_gpu", username=username))
        flash(
            f"{username} 的 GPU 分配已更新；/data 和 SSH 密钥已保留，容器可写层已重建",
            "success",
        )
        return redirect(url_for("dashboard"))

    @app.post("/users/<username>/start")
    @login_required
    def start_user(username: str) -> Any:
        user = require_user(store, username)
        if not user.active:
            flash("用户已停用，无法启动容器", "error")
        else:
            try:
                docker_service.start(user.container_id)
                flash(f"{username} 的容器已启动", "success")
            except DockerException as exc:
                flash(f"启动失败: {exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/users/<username>/stop")
    @login_required
    def stop_user(username: str) -> Any:
        user = require_user(store, username)
        try:
            docker_service.stop(user.container_id)
            flash(f"{username} 的容器已停止", "success")
        except DockerException as exc:
            flash(f"停止失败: {exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/users/<username>/disable")
    @login_required
    def disable_user(username: str) -> Any:
        user = require_user(store, username)
        store.set_active(username, False)
        try:
            docker_service.stop(user.container_id)
            flash(f"用户 {username} 已停用，容器已停止", "success")
        except DockerException as exc:
            flash(f"用户已停用，但停止容器失败: {exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/users/<username>/enable")
    @login_required
    def enable_user(username: str) -> Any:
        require_user(store, username)
        store.set_active(username, True)
        flash(f"用户 {username} 已启用，可手动启动容器", "success")
        return redirect(url_for("dashboard"))

    @app.get("/users/<username>/key")
    @login_required
    def download_key(username: str) -> Any:
        require_user(store, username)
        path = key_manager.private_key_path(username)
        if not path.is_file():
            abort(404)
        return send_file(
            path,
            as_attachment=True,
            download_name=f"id_ed25519_{username}",
            mimetype="application/octet-stream",
        )

    @app.get("/healthz")
    def healthz() -> Any:
        return {"status": "ok"}

    return app


def require_user(store: ConfigStore, username: str) -> UserRecord:
    if not USERNAME_RE.fullmatch(username):
        abort(404)
    user = store.get_user(username)
    if user is None:
        abort(404)
    return user
