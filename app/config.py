from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
ADMIN_PASSWORD_RE = re.compile(r"^[\x21-\x7E]+$")
DEFAULT_IMAGE = "ubuntu:26.04"
DEFAULT_CONTAINER_STORAGE_SIZE = "20G"


@dataclass(frozen=True)
class Settings:
    config_path: Path
    admin_password: str
    secret_key: str
    public_hostname: str
    listen_host: str
    web_port: int
    ssh_port: int
    key_dir: Path
    data_root: Path
    container_storage_size: str
    container_name_prefix: str
    container_command: tuple[str, ...]
    secure_cookies: bool

    @classmethod
    def from_file(cls, path: str | Path) -> Settings:
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file():
            raise ValueError(f"配置文件不存在: {config_path}")
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"无法读取配置文件: {exc}") from exc
        if not isinstance(raw, dict):
            raise TypeError("配置文件根节点必须是对象")
        return cls.from_mapping(raw, config_path.parent, config_path)

    @classmethod
    def bootstrap(cls, path: str | Path) -> Settings:
        config_path = Path(path).expanduser().resolve()
        return cls(
            config_path=config_path,
            admin_password="",
            secret_key=secrets.token_urlsafe(48),
            public_hostname="",
            listen_host="0.0.0.0",
            web_port=2221,
            ssh_port=2222,
            key_dir=(config_path.parent / "key_dir").resolve(),
            data_root=Path("/data"),
            container_storage_size=DEFAULT_CONTAINER_STORAGE_SIZE,
            container_name_prefix="docker-proxy-",
            container_command=(
                "/bin/sh",
                "-c",
                "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done",
            ),
            secure_cookies=False,
        )

    @classmethod
    def from_mapping(
        cls,
        raw: dict[str, Any],
        base_dir: str | Path = ".",
        config_path: str | Path | None = None,
    ) -> Settings:
        base = Path(base_dir).resolve()
        admin_password = str(raw.get("admin_password", ""))
        secret_key = str(raw.get("secret_key", ""))
        public_hostname = str(raw.get("public_hostname", ""))
        if not ADMIN_PASSWORD_RE.fullmatch(admin_password):
            raise ValueError("admin_password 只能包含英文字母、数字和常用 ASCII 符号")
        if len(secret_key) < 32 or secret_key.startswith("CHANGE_ME"):
            raise ValueError("secret_key 必须替换为至少 32 个字符的随机值")
        if not HOSTNAME_RE.fullmatch(public_hostname):
            raise ValueError("public_hostname 不是有效的主机名")
        web_port = cls._port(raw.get("web_port", 2221), "web_port")
        ssh_port = cls._port(raw.get("ssh_port", 2222), "ssh_port")
        if web_port == ssh_port:
            raise ValueError("web_port 与 ssh_port 不能相同")
        prefix = str(raw.get("container_name_prefix", "docker-proxy-"))
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", prefix):
            raise ValueError("container_name_prefix 包含无效字符")
        storage_size = str(
            raw.get("container_storage_size", DEFAULT_CONTAINER_STORAGE_SIZE)
        ).upper()
        if not re.fullmatch(r"[1-9][0-9]*[KMGT]", storage_size):
            raise ValueError("container_storage_size 必须是带 K、M、G 或 T 单位的正整数")
        command = raw.get(
            "container_command",
            [
                "/bin/sh",
                "-c",
                "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done",
            ],
        )
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ValueError("container_command 必须是非空字符串数组")
        return cls(
            config_path=(
                Path(config_path).expanduser().resolve()
                if config_path is not None
                else (base / "config.yaml").resolve()
            ),
            admin_password=admin_password,
            secret_key=secret_key,
            public_hostname=public_hostname,
            listen_host=str(raw.get("listen_host", "0.0.0.0")),
            web_port=web_port,
            ssh_port=ssh_port,
            key_dir=(
                (
                    Path(config_path).expanduser().resolve().parent
                    if config_path is not None
                    else base
                )
                / "key_dir"
            ).resolve(),
            data_root=Path("/data"),
            container_storage_size=storage_size,
            container_name_prefix=prefix,
            container_command=tuple(command),
            secure_cookies=bool(raw.get("secure_cookies", False)),
        )

    def initial_config(
        self, admin_password: str, public_hostname: str, secret_key: str
    ) -> dict[str, Any]:
        return {
            "admin_password": admin_password,
            "secret_key": secret_key,
            "public_hostname": public_hostname,
            "listen_host": self.listen_host,
            "web_port": self.web_port,
            "ssh_port": self.ssh_port,
            "key_dir": "./key_dir",
            "container_storage_size": self.container_storage_size,
            "container_name_prefix": self.container_name_prefix,
            "container_command": list(self.container_command),
            "last_image": DEFAULT_IMAGE,
            "secure_cookies": self.secure_cookies,
            "users": {},
        }

    @staticmethod
    def _port(value: Any, name: str) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是端口号") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{name} 必须在 1 到 65535 之间")
        return port
