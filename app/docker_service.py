from __future__ import annotations

import csv
import logging
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import DeviceRequest

from .config import DEFAULT_IMAGE, Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecConnection:
    exec_id: str
    socket: Any
    api: Any
    tty: bool


@dataclass(frozen=True)
class ExistingInstance:
    container_id: str
    name: str
    image: str
    status: str
    managed: bool
    gpu_ids: tuple[str, ...]


@dataclass(frozen=True)
class GPUDevice:
    index: int
    device_id: str
    name: str
    memory_mb: int


@dataclass(frozen=True)
class DockerHostInfo:
    storage_driver: str
    backing_filesystem: str
    docker_root_dir: str
    default_runtime: str
    runtimes: tuple[str, ...]


class DockerService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Any | None = None
        self._gpu_cache: tuple[float, tuple[GPUDevice, ...], str | None] | None = None
        self._gpu_lock = threading.Lock()

    def client(self) -> Any:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def ping(self) -> tuple[bool, str]:
        try:
            self.client().ping()
            return True, "Docker 已连接"
        except DockerException as exc:
            self._client = None
            return False, str(exc)

    def host_info(self) -> DockerHostInfo:
        info = self.client().info()
        driver_status = {
            str(item[0]): str(item[1])
            for item in info.get("DriverStatus", [])
            if isinstance(item, list) and len(item) == 2
        }
        runtimes = info.get("Runtimes") or {}
        return DockerHostInfo(
            storage_driver=str(info.get("Driver") or "unknown"),
            backing_filesystem=driver_status.get("Backing Filesystem", "unknown"),
            docker_root_dir=str(info.get("DockerRootDir") or "unknown"),
            default_runtime=str(info.get("DefaultRuntime") or "unknown"),
            runtimes=tuple(sorted(str(name) for name in runtimes)),
        )

    def create_instance(
        self,
        username: str,
        image: str,
        gpu_ids: tuple[str, ...],
        container_name: str | None = None,
    ) -> str:
        client = self.client()
        resolved_name = container_name or (
            f"{self.settings.container_name_prefix}{username}"
        )
        logger.info(
            "开始创建容器 username=%s image=%s container=%s gpu_ids=%s",
            username,
            image,
            resolved_name,
            gpu_ids,
        )
        try:
            client.images.get(image)
            logger.debug("使用本地镜像 image=%s", image)
        except ImageNotFound:
            logger.info("本地没有镜像，开始拉取 image=%s", image)
            try:
                client.images.pull(image)
                logger.info("镜像拉取完成 image=%s", image)
            except DockerException as exc:
                logger.exception("镜像拉取失败 image=%s", image)
                raise DockerException(
                    f"拉取镜像 {image} 失败，请检查宿主机 Docker daemon 的 DNS、网络或代理配置: {exc}"
                ) from exc
        data_dir = self._data_dir(username)
        logger.debug(
            "准备用户数据目录 username=%s host_path=%s container_path=/data",
            username,
            data_dir,
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "调用 Docker 创建容器 container=%s image=%s storage_size=%s gpu_ids=%s volume=%s:/data:rw",
            resolved_name,
            image,
            self.settings.container_storage_size,
            gpu_ids,
            data_dir,
        )
        device_requests = self._device_requests(gpu_ids)
        container = client.containers.create(
            image=image,
            name=resolved_name,
            hostname=username,
            entrypoint=[],
            command=list(self.settings.container_command),
            user="0",
            working_dir="/data",
            storage_opt={"size": self.settings.container_storage_size},
            device_requests=device_requests,
            volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
            labels={
                "docker-proxy.managed": "true",
                "docker-proxy.username": username,
                "docker-proxy.storage-size": self.settings.container_storage_size,
                "docker-proxy.gpu-ids": ",".join(gpu_ids),
            },
        )
        logger.info(
            "容器创建完成 username=%s container=%s id=%s",
            username,
            resolved_name,
            container.id,
        )
        return container.id

    def recreate_instance(
        self,
        username: str,
        image: str,
        container_id: str,
        gpu_ids: tuple[str, ...],
    ) -> str:
        client = self.client()
        old_container = client.containers.get(container_id)
        old_container.reload()
        was_running = old_container.status == "running"
        canonical_name = f"{self.settings.container_name_prefix}{username}"
        suffix = secrets.token_hex(4)
        replacement_name = f"{canonical_name}-replacement-{suffix}"
        backup_name = f"{canonical_name}-backup-{suffix}"
        replacement_id = self.create_instance(
            username,
            image,
            gpu_ids,
            container_name=replacement_name,
        )
        replacement = client.containers.get(replacement_id)
        old_renamed = False
        replacement_promoted = False
        try:
            if was_running:
                old_container.stop(timeout=10)
            old_container.rename(backup_name)
            old_renamed = True
            replacement.rename(canonical_name)
            replacement_promoted = True
            if was_running:
                replacement.start()
        except DockerException:
            logger.exception(
                "替换容器失败，开始回滚 username=%s old=%s replacement=%s",
                username,
                old_container.id,
                replacement.id,
            )
            if replacement_promoted:
                try:
                    replacement.rename(replacement_name)
                except DockerException:
                    logger.exception("回滚新容器名称失败 id=%s", replacement.id)
            if old_renamed:
                try:
                    old_container.rename(canonical_name)
                except DockerException:
                    logger.exception("恢复旧容器名称失败 id=%s", old_container.id)
            try:
                replacement.remove(force=True)
            except DockerException:
                logger.exception("清理替换容器失败 id=%s", replacement.id)
            if was_running:
                try:
                    old_container.start()
                except DockerException:
                    logger.exception("恢复旧容器运行状态失败 id=%s", old_container.id)
            raise
        try:
            old_container.remove(force=True)
        except DockerException:
            logger.exception("清理旧容器失败 id=%s name=%s", old_container.id, backup_name)
        logger.warning(
            "容器已重建 username=%s old_id=%s new_id=%s gpu_ids=%s",
            username,
            old_container.id,
            replacement.id,
            gpu_ids,
        )
        return replacement.id

    def gpu_inventory(self) -> tuple[tuple[GPUDevice, ...], str | None]:
        with self._gpu_lock:
            now = time.monotonic()
            if self._gpu_cache is not None and now - self._gpu_cache[0] < 30:
                return self._gpu_cache[1], self._gpu_cache[2]
            try:
                output = self._local_nvidia_smi()
            except (OSError, subprocess.SubprocessError):
                try:
                    output = self._docker_nvidia_smi()
                except Exception as exc:
                    message = str(exc).replace("\n", " ")[:500]
                    logger.warning("GPU 检测失败: %s", message)
                    self._gpu_cache = (now, (), message)
                    return (), message
            devices = self._parse_gpu_inventory(output)
            if not devices:
                message = "nvidia-smi 没有返回可用显卡"
                self._gpu_cache = (now, (), message)
                return (), message
            self._gpu_cache = (now, devices, None)
            logger.info(
                "检测到 NVIDIA GPU count=%d devices=%s",
                len(devices),
                [(device.index, device.device_id, device.name) for device in devices],
            )
            return devices, None

    @staticmethod
    def _local_nvidia_smi() -> bytes:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return result.stdout

    def _docker_nvidia_smi(self) -> bytes:
        client = self.client()
        try:
            client.images.get(DEFAULT_IMAGE)
        except ImageNotFound as exc:
            raise DockerException(
                f"GPU 检测需要本地镜像 {DEFAULT_IMAGE}，当前尚未拉取"
            ) from exc
        container = client.containers.create(
            image=DEFAULT_IMAGE,
            entrypoint=[],
            command=[
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            network_disabled=True,
            device_requests=[DeviceRequest(count=-1, capabilities=[["gpu"]])],
            environment={"NVIDIA_DRIVER_CAPABILITIES": "utility"},
        )
        try:
            container.start()
            result = container.wait(timeout=15)
            output = container.logs(stdout=True, stderr=True)
            status_code = int(result.get("StatusCode", 1))
            if status_code != 0:
                detail = output.decode("utf-8", "replace").strip()
                raise DockerException(
                    f"GPU 探测容器退出状态 {status_code}: {detail}"
                )
            return output
        finally:
            try:
                container.remove(force=True)
            except DockerException:
                logger.exception("清理 GPU 探测容器失败 id=%s", container.id)

    @staticmethod
    def _parse_gpu_inventory(output: bytes) -> tuple[GPUDevice, ...]:
        devices = []
        for row in csv.reader(output.decode("utf-8", "replace").splitlines()):
            if len(row) != 4:
                continue
            try:
                index = int(row[0].strip())
                memory_mb = int(float(row[3].strip()))
            except ValueError:
                continue
            device_id = row[1].strip()
            if not device_id.startswith("GPU-"):
                continue
            devices.append(
                GPUDevice(
                    index=index,
                    device_id=device_id,
                    name=row[2].strip(),
                    memory_mb=memory_mb,
                )
            )
        return tuple(sorted(devices, key=lambda device: device.index))

    @staticmethod
    def _device_requests(gpu_ids: tuple[str, ...]) -> list[DeviceRequest]:
        if not gpu_ids:
            return []
        if gpu_ids == ("all",):
            return [DeviceRequest(count=-1, capabilities=[["gpu"]])]
        return [
            DeviceRequest(device_ids=list(gpu_ids), capabilities=[["gpu"]])
        ]

    def remove_instance(self, container_id: str) -> None:
        try:
            container = self.client().containers.get(container_id)
            logger.warning(
                "删除容器 name=%s id=%s status=%s",
                container.name,
                container.id,
                container.status,
            )
            container.remove(force=True)
            logger.info("容器已删除 name=%s id=%s", container.name, container.id)
        except NotFound:
            logger.info("待删除容器不存在 id=%s", container_id)
            return

    def existing_instance(self, username: str) -> ExistingInstance | None:
        name = f"{self.settings.container_name_prefix}{username}"
        try:
            container = self.client().containers.get(name)
        except NotFound:
            logger.debug("未发现同名容器 username=%s container=%s", username, name)
            return None
        config = container.attrs.get("Config") or {}
        labels = config.get("Labels") or {}
        image = str(config.get("Image") or "unknown")
        state = container.attrs.get("State") or {}
        status = str(state.get("Status") or container.status or "unknown")
        managed = (
            labels.get("docker-proxy.managed") == "true"
            and labels.get("docker-proxy.username") == username
        )
        gpu_ids = self._gpu_ids_from_host_config(container.attrs.get("HostConfig") or {})
        instance = ExistingInstance(
            container_id=container.id,
            name=container.name,
            image=image,
            status=status,
            managed=managed,
            gpu_ids=gpu_ids,
        )
        logger.warning(
            "发现同名容器 username=%s container=%s id=%s image=%s status=%s managed=%s gpu_ids=%s",
            username,
            instance.name,
            instance.container_id,
            instance.image,
            instance.status,
            instance.managed,
            instance.gpu_ids,
        )
        return instance

    @staticmethod
    def _gpu_ids_from_host_config(host_config: dict[str, Any]) -> tuple[str, ...]:
        for device_request in host_config.get("DeviceRequests") or []:
            capabilities = device_request.get("Capabilities") or []
            if not any("gpu" in group for group in capabilities):
                continue
            device_ids = tuple(str(value) for value in device_request.get("DeviceIDs") or [])
            if device_ids:
                return device_ids
            if int(device_request.get("Count") or 0) == -1:
                return ("all",)
        return ()

    def status(self, container_id: str) -> str:
        try:
            container = self.client().containers.get(container_id)
            container.reload()
            return str(container.status)
        except NotFound:
            return "missing"
        except DockerException:
            return "unavailable"

    def start(self, container_id: str) -> None:
        container = self.client().containers.get(container_id)
        container.start()

    def stop(self, container_id: str) -> None:
        container = self.client().containers.get(container_id)
        container.stop(timeout=10)

    def open_exec(
        self,
        container_id: str,
        command: list[str],
        term_type: str | None,
        term_size: tuple[int, int, int, int] | None,
        environment: dict[str, str] | None = None,
    ) -> ExecConnection:
        api = self.client().api
        tty = term_type is not None
        exec_environment = {"TERM": term_type or "xterm-256color"}
        if environment:
            exec_environment.update(environment)
        result = api.exec_create(
            container=container_id,
            cmd=command,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=tty,
            user="root",
            environment=exec_environment,
        )
        exec_id = result["Id"]
        sock = api.exec_start(exec_id, tty=tty, socket=True)
        if term_size:
            width, height = term_size[0], term_size[1]
            if width and height:
                api.exec_resize(exec_id, height=height, width=width)
        return ExecConnection(exec_id, sock, api, tty)

    def signal_exec(
        self,
        container_id: str,
        pid_file: str,
        signal_name: str,
    ) -> None:
        allowed = {
            "ABRT",
            "ALRM",
            "FPE",
            "HUP",
            "ILL",
            "INT",
            "KILL",
            "PIPE",
            "QUIT",
            "SEGV",
            "TERM",
            "TSTP",
            "USR1",
            "USR2",
        }
        if signal_name not in allowed:
            raise ValueError(f"不支持的信号: {signal_name}")
        script = 'pid=$(cat "$1" 2>/dev/null) || exit 0; kill -"$2" "$pid"'
        self.client().containers.get(container_id).exec_run(
            ["/bin/sh", "-c", script, "docker-proxy-signal", pid_file, signal_name],
            user="root",
        )

    def remove_exec_pid_file(self, container_id: str, pid_file: str) -> None:
        self.client().containers.get(container_id).exec_run(
            ["/bin/sh", "-c", 'rm -f "$1"', "docker-proxy-cleanup", pid_file],
            user="root",
        )

    def exec_exit_code(self, connection: ExecConnection) -> int:
        result = connection.api.exec_inspect(connection.exec_id)
        code = result.get("ExitCode")
        return int(code) if code is not None else 0

    @staticmethod
    def resize_exec(connection: ExecConnection, width: int, height: int) -> None:
        if width > 0 and height > 0:
            connection.api.exec_resize(
                connection.exec_id,
                height=height,
                width=width,
            )

    def _data_dir(self, username: str) -> Path:
        root = self.settings.data_root.resolve()
        target = (root / username).resolve()
        if target.parent != root:
            raise ValueError("用户数据目录超出 data_root")
        return target
