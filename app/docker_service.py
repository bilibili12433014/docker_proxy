from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecConnection:
    exec_id: str
    socket: Any
    api: Any


@dataclass(frozen=True)
class ExistingInstance:
    container_id: str
    name: str
    image: str
    status: str
    managed: bool


class DockerService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Any | None = None

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

    def create_instance(self, username: str, image: str) -> str:
        client = self.client()
        container_name = f"{self.settings.container_name_prefix}{username}"
        logger.info(
            "开始创建容器 username=%s image=%s container=%s",
            username,
            image,
            container_name,
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
            "调用 Docker 创建容器 container=%s image=%s storage_size=%s volume=%s:/data:rw",
            container_name,
            image,
            self.settings.container_storage_size,
            data_dir,
        )
        container = client.containers.create(
            image=image,
            name=container_name,
            hostname=username,
            entrypoint=[],
            command=list(self.settings.container_command),
            user="0",
            working_dir="/data",
            storage_opt={"size": self.settings.container_storage_size},
            volumes={str(data_dir): {"bind": "/data", "mode": "rw"}},
            labels={
                "docker-proxy.managed": "true",
                "docker-proxy.username": username,
            },
        )
        logger.info(
            "容器创建完成 username=%s container=%s id=%s",
            username,
            container_name,
            container.id,
        )
        return container.id

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
        instance = ExistingInstance(
            container_id=container.id,
            name=container.name,
            image=image,
            status=status,
            managed=managed,
        )
        logger.warning(
            "发现同名容器 username=%s container=%s id=%s image=%s status=%s managed=%s",
            username,
            instance.name,
            instance.container_id,
            instance.image,
            instance.status,
            instance.managed,
        )
        return instance

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
    ) -> ExecConnection:
        api = self.client().api
        result = api.exec_create(
            container=container_id,
            cmd=command,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            user="root",
            environment={"TERM": term_type or "xterm-256color"},
        )
        exec_id = result["Id"]
        sock = api.exec_start(exec_id, tty=True, socket=True)
        if term_size:
            width, height = term_size[0], term_size[1]
            if width and height:
                api.exec_resize(exec_id, height=height, width=width)
        return ExecConnection(exec_id, sock, api)

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
