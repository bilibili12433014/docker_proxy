from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import asyncssh
from docker.errors import DockerException

from .config_store import ConfigStore
from .docker_service import DockerService, ExecConnection
from .keys import KeyManager, normalize_public_key

logger = logging.getLogger(__name__)
APT_MIRROR_SETUP = (
    "if command -v apt-get >/dev/null 2>&1 && [ -r /etc/os-release ]; then "
    ". /etc/os-release; "
    "if [ \"${ID:-}\" = ubuntu ]; then "
    "codename=${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}; "
    "arch=$(dpkg --print-architecture 2>/dev/null || true); "
    "case \"$arch\" in "
    "amd64|i386) repo=ubuntu; security=http://security.ubuntu.com/ubuntu/ ;; "
    "*) repo=ubuntu-ports; security=http://ports.ubuntu.com/ubuntu-ports/ ;; "
    "esac; "
    "mirror=\"http://mirrors4.tuna.tsinghua.edu.cn/$repo\"; "
    "if [ -n \"$codename\" ]; then "
    "if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then "
    "printf 'Types: deb\\nURIs: %s\\nSuites: %s %s-updates %s-backports\\nComponents: main restricted universe multiverse\\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\\n\\nTypes: deb\\nURIs: %s\\nSuites: %s-security\\nComponents: main restricted universe multiverse\\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\\n' "
    "\"$mirror\" \"$codename\" \"$codename\" \"$codename\" \"$security\" \"$codename\" "
    "> /etc/apt/sources.list.d/ubuntu.sources; "
    "else "
    "printf 'deb %s %s main restricted universe multiverse\\ndeb %s %s-updates main restricted universe multiverse\\ndeb %s %s-backports main restricted universe multiverse\\ndeb %s %s-security main restricted universe multiverse\\n' "
    "\"$mirror\" \"$codename\" \"$mirror\" \"$codename\" \"$mirror\" \"$codename\" \"$security\" \"$codename\" "
    "> /etc/apt/sources.list; "
    "fi; "
    "fi; "
    "elif [ \"${ID:-}\" = debian ]; then "
    "for apt_source in /etc/apt/sources.list /etc/apt/sources.list.d/*.list "
    "/etc/apt/sources.list.d/*.sources; do "
    "[ -f \"$apt_source\" ] || continue; "
    "sed -Ei "
    "-e 's#https?://(security\\.debian\\.org|deb\\.debian\\.org)/debian-security/?#http://mirrors4.tuna.tsinghua.edu.cn/debian-security/#g' "
    "-e 's#https?://deb\\.debian\\.org/debian/?#http://mirrors4.tuna.tsinghua.edu.cn/debian/#g' "
    "\"$apt_source\"; "
    "done; "
    "fi; "
    "fi; "
)
TMUX_INSTALL = (
    "if ! command -v tmux >/dev/null 2>&1; then "
    "printf 'tmux 未安装，正在自动安装...\\r\\n'; "
    "if command -v apt-get >/dev/null 2>&1; then "
    "printf 'APT 已使用国内镜像，正在更新软件列表...\\r\\n'; "
    "apt-get -o Acquire::ForceIPv4=true update && "
    "DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y tmux; "
    "elif command -v apk >/dev/null 2>&1; then apk add --no-cache tmux; "
    "elif command -v dnf >/dev/null 2>&1; then dnf install -y tmux; "
    "elif command -v yum >/dev/null 2>&1; then yum install -y tmux; "
    "elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm tmux; "
    "else printf '当前镜像没有受支持的包管理器，无法自动安装 tmux。\\r\\n'; fi; "
    "fi; "
)
SHELL_SETUP = (
    "if [ -x /bin/bash ]; then "
    "export SHELL=/bin/bash; "
    "printf '%s\\n' "
    "\"PS1='\\\\[\\\\033[1;32m\\\\]\\\\u@\\\\h\\\\[\\\\033[0m\\\\]:\\\\[\\\\033[1;34m\\\\]\\\\w\\\\[\\\\033[0m\\\\]# '\" "
    "\"alias ls='ls --color=auto'\" "
    "'shopt -s checkwinsize' > /root/.docker_proxy_bashrc; "
    "shell_command='/bin/bash --noprofile --rcfile /root/.docker_proxy_bashrc -i'; "
    "else "
    "export SHELL=/bin/sh; "
    "green=$(printf '\\033[1;32m'); blue=$(printf '\\033[1;34m'); reset=$(printf '\\033[0m'); "
    "PS1=\"${green}root@$(hostname)${reset}:${blue}\"'${PWD}'\"${reset}# \"; "
    "export PS1; shell_command='/bin/sh -i'; "
    "fi; "
)
TMUX_START = (
    "if command -v tmux >/dev/null 2>&1; then "
    "printf '%s\\n' 'set -g mouse on' 'set -g history-limit 100000' "
    "'set -g default-terminal \"screen-256color\"' > /root/.tmux.conf; "
    "if tmux has-session -t workspace 2>/dev/null; then "
    "shell_version=$(tmux show-options -t workspace -qv @docker_proxy_shell_version 2>/dev/null || true); "
    "if [ \"$shell_version\" != 2 ]; then "
    "printf '正在升级终端会话配置，原 tmux 窗口将继续保留...\\r\\n'; "
    "tmux new-window -d -t workspace -n docker-proxy-shell \"$shell_command\"; "
    "tmux select-window -t workspace:docker-proxy-shell; "
    "tmux set-option -t workspace @docker_proxy_shell_version 2; "
    "fi; "
    "fi; "
    "if ! tmux has-session -t workspace 2>/dev/null; then "
    "tmux -f /root/.tmux.conf new-session -d -s workspace \"$shell_command\"; "
    "tmux set-option -t workspace @docker_proxy_shell_version 2; "
    "fi; "
    "tmux set-option -g mouse on; "
    "tmux set-option -g history-limit 100000; "
    "exec tmux attach-session -t workspace; "
    "fi; "
    "printf 'tmux 不可用，已回退到普通 shell。\\r\\n'; exec $shell_command"
)
INTERACTIVE_SHELL = [
    "/bin/sh",
    "-c",
    APT_MIRROR_SETUP + TMUX_INSTALL + SHELL_SETUP + TMUX_START,
]


class GatewaySSHServer(asyncssh.SSHServer):
    def __init__(self, store: ConfigStore):
        self.store = store

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        user = self.store.get_active_user(username)
        if user is None:
            return False
        offered = normalize_public_key(key.export_public_key("openssh"))
        return bool(offered) and offered == normalize_public_key(user.public_key)


class SSHGateway:
    def __init__(
        self,
        store: ConfigStore,
        docker_service: DockerService,
        key_manager: KeyManager,
    ):
        self.store = store
        self.docker = docker_service
        self.key_manager = key_manager
        self._acceptor: Any | None = None

    async def start(self, host: str, port: int) -> Any:
        host_key = self.key_manager.ensure_host_key()
        self._acceptor = await asyncssh.create_server(
            lambda: GatewaySSHServer(self.store),
            host,
            port,
            server_host_keys=[str(host_key)],
            process_factory=self.handle_process,
            encoding=None,
            line_editor=False,
        )
        return self._acceptor

    async def close(self) -> None:
        if self._acceptor is not None:
            self._acceptor.close()
            await self._acceptor.wait_closed()

    async def handle_process(self, process: asyncssh.SSHServerProcess[bytes]) -> None:
        username = process.get_extra_info("username")
        user = self.store.get_active_user(username)
        if user is None:
            await self._fail(process, "用户不存在或已停用")
            return
        status = await asyncio.to_thread(self.docker.status, user.container_id)
        if status != "running":
            await self._fail(process, f"容器当前状态为 {status}，请先在管理界面启动")
            return
        requested = process.command
        if isinstance(requested, bytes):
            requested = requested.decode("utf-8", "replace")
        command = ["/bin/sh", "-lc", requested] if requested else INTERACTIVE_SHELL
        try:
            connection = await asyncio.to_thread(
                self.docker.open_exec,
                user.container_id,
                command,
                process.term_type,
                process.term_size,
            )
        except (DockerException, OSError, ValueError) as exc:
            logger.exception("无法为用户 %s 创建 Docker exec", username)
            await self._fail(process, f"无法进入容器: {exc}")
            return
        logger.info(
            "Docker Exec 已连接 username=%s container_id=%s exec_id=%s command=%s",
            username,
            user.container_id,
            connection.exec_id,
            command,
        )
        input_task = asyncio.create_task(self._copy_ssh_to_docker(process, connection))
        output_task = asyncio.create_task(self._copy_docker_to_ssh(process, connection))
        try:
            await output_task
        finally:
            input_task.cancel()
            try:
                connection.socket.close()
            except OSError:
                pass
            input_result = await asyncio.gather(input_task, return_exceptions=True)
            if input_result and isinstance(input_result[0], Exception) and not isinstance(
                input_result[0], asyncio.CancelledError
            ):
                logger.error(
                    "SSH 输入转发任务异常 username=%s exec_id=%s error=%r",
                    username,
                    connection.exec_id,
                    input_result[0],
                )
        try:
            exit_code = await asyncio.to_thread(self.docker.exec_exit_code, connection)
        except DockerException:
            exit_code = 1
            logger.exception(
                "读取 Docker Exec 退出状态失败 username=%s exec_id=%s",
                username,
                connection.exec_id,
            )
        logger.info(
            "Docker Exec 已结束 username=%s exec_id=%s exit_code=%d",
            username,
            connection.exec_id,
            exit_code,
        )
        process.exit(exit_code)

    async def _copy_ssh_to_docker(
        self,
        process: asyncssh.SSHServerProcess[bytes],
        connection: ExecConnection,
    ) -> None:
        username = process.get_extra_info("username")
        try:
            while True:
                try:
                    data = await process.stdin.read(32768)
                except asyncssh.TerminalSizeChanged as exc:
                    logger.info(
                        "终端尺寸变化 username=%s exec_id=%s width=%d height=%d pixels=%dx%d",
                        username,
                        connection.exec_id,
                        exc.width,
                        exc.height,
                        exc.pixwidth,
                        exc.pixheight,
                    )
                    await asyncio.to_thread(
                        self.docker.resize_exec,
                        connection,
                        exc.width,
                        exc.height,
                    )
                    continue
                if not data:
                    logger.info(
                        "SSH 输入结束 username=%s exec_id=%s",
                        username,
                        connection.exec_id,
                    )
                    return
                logger.debug(
                    "转发 SSH->Docker username=%s exec_id=%s bytes=%d",
                    username,
                    connection.exec_id,
                    len(data),
                )
                await asyncio.to_thread(self._write_all, connection.socket, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "SSH->Docker 转发失败 username=%s exec_id=%s",
                username,
                connection.exec_id,
            )
            raise
        finally:
            await asyncio.to_thread(self._shutdown_write, connection.socket)

    @staticmethod
    async def _copy_docker_to_ssh(
        process: asyncssh.SSHServerProcess[bytes], connection: ExecConnection
    ) -> None:
        username = process.get_extra_info("username")
        try:
            while True:
                data = await asyncio.to_thread(
                    SSHGateway._read_socket, connection.socket, 32768
                )
                if not data:
                    logger.info(
                        "Docker 输出结束 username=%s exec_id=%s",
                        username,
                        connection.exec_id,
                    )
                    return
                logger.debug(
                    "转发 Docker->SSH username=%s exec_id=%s bytes=%d",
                    username,
                    connection.exec_id,
                    len(data),
                )
                process.stdout.write(data)
                await process.stdout.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Docker->SSH 转发失败 username=%s exec_id=%s",
                username,
                connection.exec_id,
            )
            raise

    @staticmethod
    def _write_all(sock: Any, data: bytes) -> None:
        raw_socket = getattr(sock, "_sock", sock)
        sendall = getattr(raw_socket, "sendall", None)
        if sendall is not None:
            sendall(data)
            return
        view = memoryview(data)
        while view:
            written = sock.write(view)
            if written is None:
                return
            if written <= 0:
                raise OSError("Docker exec 连接已关闭")
            view = view[written:]

    @staticmethod
    def _read_socket(sock: Any, size: int) -> bytes:
        raw_socket = getattr(sock, "_sock", sock)
        recv = getattr(raw_socket, "recv", None)
        if recv is not None:
            return recv(size)
        return sock.read(size)

    @staticmethod
    def _shutdown_write(sock: Any) -> None:
        raw_socket = getattr(sock, "_sock", sock)
        shutdown = getattr(raw_socket, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown(socket.SHUT_WR)
            except OSError:
                pass

    @staticmethod
    async def _fail(process: asyncssh.SSHServerProcess[bytes], message: str) -> None:
        process.stderr.write((message + "\r\n").encode("utf-8"))
        await process.stderr.drain()
        process.exit(1)
