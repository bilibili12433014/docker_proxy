from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

import asyncssh


@dataclass(frozen=True)
class UserKey:
    private_path: Path
    public_path: Path
    public_key: str


def normalize_public_key(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii")
    fields = value.strip().split()
    if len(fields) < 2:
        return ""
    return f"{fields[0]} {fields[1]}"


class KeyManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def host_key_path(self) -> Path:
        return self.root / "ssh_host_ed25519_key"

    def ensure_host_key(self) -> Path:
        if not self.host_key_path.exists():
            key = asyncssh.generate_private_key("ssh-ed25519")
            self._write_private(self.host_key_path, key.export_private_key("openssh"))
        return self.host_key_path

    def create_user_key(self, username: str) -> UserKey:
        user_dir = self.root / "users" / username
        if user_dir.exists():
            raise FileExistsError(f"用户 {username} 的密钥目录已存在")
        user_dir.mkdir(parents=True, mode=0o700)
        private_path = user_dir / "id_ed25519"
        public_path = user_dir / "id_ed25519.pub"
        key = asyncssh.generate_private_key("ssh-ed25519")
        private_data = key.export_private_key("openssh")
        public_data = key.export_public_key("openssh")
        self._write_private(private_path, private_data)
        public_path.write_bytes(public_data)
        return UserKey(private_path, public_path, normalize_public_key(public_data))

    def reset_user_key(self, username: str) -> UserKey:
        user_dir = self.root / "users" / username
        user_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        private_path = user_dir / "id_ed25519"
        public_path = user_dir / "id_ed25519.pub"
        suffix = secrets.token_hex(8)
        temporary_private = user_dir / f".id_ed25519.{suffix}.tmp"
        temporary_public = user_dir / f".id_ed25519.{suffix}.pub.tmp"
        key = asyncssh.generate_private_key("ssh-ed25519")
        private_data = key.export_private_key("openssh")
        public_data = key.export_public_key("openssh")
        try:
            self._write_private(temporary_private, private_data)
            temporary_public.write_bytes(public_data)
            os.replace(temporary_private, private_path)
            os.replace(temporary_public, public_path)
        finally:
            temporary_private.unlink(missing_ok=True)
            temporary_public.unlink(missing_ok=True)
        return UserKey(private_path, public_path, normalize_public_key(public_data))

    def private_key_path(self, username: str) -> Path:
        return self.root / "users" / username / "id_ed25519"

    def remove_user_key(self, username: str) -> bool:
        user_dir = (self.root / "users" / username).resolve()
        users_root = (self.root / "users").resolve()
        if user_dir.parent != users_root:
            raise ValueError("密钥目录超出允许范围")
        if user_dir.exists():
            shutil.rmtree(user_dir)
            return True
        return False

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        path.write_bytes(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
