from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_CONTAINER_STORAGE_SIZE, DEFAULT_IMAGE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserRecord:
    username: str
    image: str
    container_id: str
    public_key: str
    gpu_ids: tuple[str, ...]
    active: bool
    created_at: str


class ConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        if self.path.is_file():
            self._migrate_config()
            self.list_users()

    def is_initialized(self) -> bool:
        return self.path.is_file()

    def initialize(self, data: dict[str, Any]) -> bool:
        with self._lock:
            if self.path.exists():
                return False
            self._write(data)
        return True

    def admin_password(self) -> str:
        value = self._read().get("admin_password", "")
        return value if isinstance(value, str) else ""

    def public_hostname(self) -> str:
        value = self._read().get("public_hostname", "")
        return value if isinstance(value, str) else ""

    def last_image(self) -> str:
        value = self._read().get("last_image", DEFAULT_IMAGE)
        return value if isinstance(value, str) and value else DEFAULT_IMAGE

    def set_last_image(self, image: str) -> None:
        with self._lock:
            data = self._read()
            data["last_image"] = image
            self._write(data)

    def add_user(
        self,
        username: str,
        image: str,
        container_id: str,
        public_key: str,
        gpu_ids: tuple[str, ...],
    ) -> UserRecord:
        created_at = datetime.now(UTC).isoformat()
        record = UserRecord(
            username=username,
            image=image,
            container_id=container_id,
            public_key=public_key,
            gpu_ids=gpu_ids,
            active=True,
            created_at=created_at,
        )
        with self._lock:
            data = self._read()
            users = self._users_mapping(data)
            if username in users:
                raise ValueError(f"用户 {username} 已存在")
            users[username] = self._serialize(record)
            self._write(data)
        return record

    def get_user(self, username: str) -> UserRecord | None:
        with self._lock:
            users = self._read_users()
            value = users.get(username)
            return self._record(username, value) if value is not None else None

    def get_active_user(self, username: str) -> UserRecord | None:
        user = self.get_user(username)
        return user if user is not None and user.active else None

    def list_users(self) -> list[UserRecord]:
        with self._lock:
            records = [
                self._record(username, value)
                for username, value in self._read_users().items()
            ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def set_active(self, username: str, active: bool) -> bool:
        with self._lock:
            data = self._read()
            users = self._users_mapping(data)
            value = users.get(username)
            if value is None:
                return False
            self._record(username, value)
            value["active"] = active
            self._write(data)
        return True

    def reset_public_key(self, username: str, public_key: str) -> UserRecord:
        with self._lock:
            data = self._read()
            users = self._users_mapping(data)
            value = users.get(username)
            if value is None:
                raise ValueError(f"用户 {username} 不存在")
            self._record(username, value)
            value["public_key"] = public_key
            value["active"] = True
            self._write(data)
            return self._record(username, value)

    def _read_users(self) -> dict[str, dict[str, Any]]:
        return self._users_mapping(self._read())

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"users": {}}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"无法读取配置文件: {exc}") from exc
        if not isinstance(data, dict):
            raise TypeError("配置文件根节点必须是对象")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                yaml.safe_dump(
                    data,
                    output,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _migrate_config(self) -> None:
        with self._lock:
            data = self._read()
            changed = False
            if "data_root" in data:
                previous = data.pop("data_root")
                changed = True
                logger.info(
                    "已移除旧配置项 data_root=%s，用户数据目录固定为 /data",
                    previous,
                )
            if "container_storage_size" not in data:
                data["container_storage_size"] = DEFAULT_CONTAINER_STORAGE_SIZE
                changed = True
                logger.info(
                    "已添加配置项 container_storage_size=%s",
                    DEFAULT_CONTAINER_STORAGE_SIZE,
                )
            users = self._users_mapping(data)
            for username, value in users.items():
                if "gpu_ids" not in value:
                    value["gpu_ids"] = []
                    changed = True
                    logger.info("已为用户添加 GPU 配置 username=%s", username)
            if changed:
                self._write(data)

    @staticmethod
    def _users_mapping(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        users = data.setdefault("users", {})
        if not isinstance(users, dict):
            raise TypeError("配置项 users 必须是对象")
        for username, value in users.items():
            if not isinstance(username, str) or not isinstance(value, dict):
                raise TypeError("users 中的每个用户必须是对象")
        return users

    @staticmethod
    def _record(username: str, value: dict[str, Any]) -> UserRecord:
        required = {
            "image",
            "container_id",
            "public_key",
            "gpu_ids",
            "active",
            "created_at",
        }
        if required - value.keys():
            raise ValueError(f"用户 {username} 的配置不完整")
        if not all(
            isinstance(value[field], str)
            for field in required - {"active", "gpu_ids"}
        ):
            raise ValueError(f"用户 {username} 的字符串字段格式错误")
        gpu_ids = value["gpu_ids"]
        if not isinstance(gpu_ids, list) or not all(
            isinstance(item, str) and item for item in gpu_ids
        ):
            raise ValueError(f"用户 {username} 的 gpu_ids 格式错误")
        if not isinstance(value["active"], bool):
            raise TypeError(f"用户 {username} 的 active 必须是布尔值")
        return UserRecord(
            username=username,
            image=value["image"],
            container_id=value["container_id"],
            public_key=value["public_key"],
            gpu_ids=tuple(gpu_ids),
            active=value["active"],
            created_at=value["created_at"],
        )

    @staticmethod
    def _serialize(record: UserRecord) -> dict[str, Any]:
        return {
            "image": record.image,
            "container_id": record.container_id,
            "public_key": record.public_key,
            "gpu_ids": list(record.gpu_ids),
            "active": record.active,
            "created_at": record.created_at,
        }
