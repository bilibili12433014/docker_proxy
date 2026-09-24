from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import secrets
import shlex
import socket
from functools import partial
from pathlib import Path
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


def tool_install(
    command: str,
    apt_package: str,
    apk_package: str,
    rpm_package: str,
    pacman_package: str,
) -> str:
    log_path = f"/tmp/docker_proxy_{command}_install.log"
    return (
        f"if ! command -v {command} >/dev/null 2>&1; then "
        + APT_MIRROR_SETUP
        + f"log={shlex.quote(log_path)}; "
        "if command -v apt-get >/dev/null 2>&1; then "
        "apt-get -o Acquire::ForceIPv4=true update >\"$log\" 2>&1 && "
        f"DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y {apt_package} >>\"$log\" 2>&1; "
        f"elif command -v apk >/dev/null 2>&1; then apk add --no-cache {apk_package} >\"$log\" 2>&1; "
        f"elif command -v dnf >/dev/null 2>&1; then dnf install -y {rpm_package} >\"$log\" 2>&1; "
        f"elif command -v yum >/dev/null 2>&1; then yum install -y {rpm_package} >\"$log\" 2>&1; "
        f"elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm {pacman_package} >\"$log\" 2>&1; "
        "fi; "
        f"if ! command -v {command} >/dev/null 2>&1; then "
        f"printf '无法安装 {command}，安装日志如下：\\n' >&2; "
        "tail -n 30 \"$log\" >&2 2>/dev/null || true; exit 127; fi; "
        "fi; "
    )


RSYNC_INSTALL = tool_install("rsync", "rsync", "rsync", "rsync", "rsync")
GIT_INSTALL = tool_install("git-upload-pack", "git", "git", "git", "git")
NC_INSTALL = tool_install(
    "nc",
    "netcat-openbsd",
    "netcat-openbsd",
    "nmap-ncat",
    "openbsd-netcat",
)
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def command_setup(command: str) -> str:
    stripped = command.lstrip()
    if stripped.startswith("rsync "):
        return RSYNC_INSTALL
    if stripped.startswith(
        ("git-upload-pack ", "git-receive-pack ", "git-upload-archive ")
    ):
        return GIT_INSTALL
    return ""


def session_environment(environment: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in dict(environment or {}).items():
        key = str(key)
        value = str(value)
        if (
            ENVIRONMENT_NAME_RE.fullmatch(key)
            and len(key) <= 128
            and len(value) <= 8192
            and "\x00" not in value
        ):
            result[key] = value
    return result


def process_environment(process: asyncssh.SSHServerProcess[bytes]) -> dict[str, str]:
    result = session_environment(process.env)
    peer = process.get_extra_info("peername")
    local = process.get_extra_info("sockname")
    if (
        isinstance(peer, tuple)
        and len(peer) >= 2
        and isinstance(local, tuple)
        and len(local) >= 2
    ):
        peer_host, peer_port = str(peer[0]), str(peer[1])
        local_host, local_port = str(local[0]), str(local[1])
        result.setdefault(
            "SSH_CONNECTION",
            f"{peer_host} {peer_port} {local_host} {local_port}",
        )
        result.setdefault("SSH_CLIENT", f"{peer_host} {peer_port} {local_port}")
    result.setdefault("USER", "root")
    result.setdefault("LOGNAME", "root")
    result.setdefault("HOME", "/root")
    return result


class GatewaySSHServer(asyncssh.SSHServer):
    def __init__(self, store: ConfigStore, forward_handler: Any):
        self.store = store
        self.forward_handler = forward_handler
        self.username = ""

    def begin_auth(self, username: str) -> bool:
        self.username = username
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        user = self.store.get_active_user(username)
        if user is None:
            return False
        offered = normalize_public_key(key.export_public_key("openssh"))
        return bool(offered) and offered == normalize_public_key(user.public_key)

    def connection_requested(
        self,
        dest_host: str,
        dest_port: int,
        orig_host: str,
        orig_port: int,
    ) -> Any:
        if self.store.get_active_user(self.username) is None:
            raise asyncssh.ChannelOpenError(
                asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED,
                "用户不存在或已停用",
            )
        return partial(
            self.forward_handler,
            self.username,
            dest_host,
            dest_port,
            orig_host,
            orig_port,
        )

    @staticmethod
    def server_requested(listen_host: str, listen_port: int) -> bool:
        return listen_host in {"localhost", "127.0.0.1", "::1"}


class GatewaySFTPServer(asyncssh.SFTPServer):
    def __init__(
        self,
        channel: asyncssh.SSHServerChannel,
        store: ConfigStore,
        data_root: Path,
    ):
        username = str(channel.get_extra_info("username") or "")
        if store.get_active_user(username) is None:
            raise asyncssh.SFTPPermissionDenied("用户不存在或已停用")
        root = data_root.resolve()
        user_root = (root / username).resolve()
        if user_root.parent != root:
            raise asyncssh.SFTPPermissionDenied("数据目录无效")
        user_root.mkdir(parents=True, exist_ok=True)
        self._user_root = os.fsencode(user_root)
        super().__init__(channel)

    def map_path(self, path: bytes) -> bytes:
        virtual_path = posixpath.normpath(posixpath.join(b"/", path))
        if virtual_path == b"/data":
            virtual_path = b"/"
        elif virtual_path.startswith(b"/data/"):
            virtual_path = virtual_path[len(b"/data") :]
        local_path = os.path.realpath(
            os.path.join(self._user_root, virtual_path.lstrip(b"/"))
        )
        if local_path != self._user_root and not local_path.startswith(
            self._user_root + os.sep.encode()
        ):
            raise asyncssh.SFTPPermissionDenied("路径超出用户数据目录")
        return local_path

    def reverse_map_path(self, path: bytes) -> bytes:
        local_path = os.path.realpath(path)
        if local_path == self._user_root:
            return b"/data"
        prefix = self._user_root + os.sep.encode()
        if not local_path.startswith(prefix):
            raise asyncssh.SFTPNoSuchFile("文件不存在")
        relative = local_path[len(self._user_root) :].replace(os.sep.encode(), b"/")
        return b"/data" + relative


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
            lambda: GatewaySSHServer(self.store, self.forward_connection),
            host,
            port,
            server_host_keys=[str(host_key)],
            process_factory=self.handle_process,
            sftp_factory=lambda channel: GatewaySFTPServer(
                channel,
                self.store,
                self.docker.settings.data_root,
            ),
            server_version="OpenSSH_9.9p2",
            keepalive_interval=30,
            keepalive_count_max=3,
            agent_forwarding=True,
            encoding=None,
            line_editor=False,
        )
        return self._acceptor

    async def close(self) -> None:
        if self._acceptor is not None:
            self._acceptor.close()
            await self._acceptor.wait_closed()

    async def forward_connection(
        self,
        username: str,
        dest_host: str,
        dest_port: int,
        orig_host: str,
        orig_port: int,
        reader: Any,
        writer: Any,
    ) -> None:
        user = self.store.get_active_user(username)
        if (
            user is None
            or not 1 <= dest_port <= 65535
            or not re.fullmatch(r"[A-Za-z0-9._:%-]{1,253}", dest_host)
            or dest_host.startswith("-")
        ):
            writer.close()
            return
        status = await asyncio.to_thread(self.docker.status, user.container_id)
        if status != "running":
            writer.close()
            return
        command = [
            "/bin/sh",
            "-lc",
            NC_INSTALL + f"exec nc {shlex.quote(dest_host)} {dest_port}",
        ]
        try:
            connection = await asyncio.to_thread(
                self.docker.open_exec,
                user.container_id,
                command,
                None,
                None,
                None,
            )
        except (DockerException, OSError, ValueError):
            logger.exception(
                "TCP 转发建立失败 username=%s destination=%s:%d",
                username,
                dest_host,
                dest_port,
            )
            writer.close()
            return
        logger.info(
            "TCP 转发已连接 username=%s origin=%s:%d destination=%s:%d exec_id=%s",
            username,
            orig_host,
            orig_port,
            dest_host,
            dest_port,
            connection.exec_id,
        )
        input_task = asyncio.create_task(
            self._copy_forward_to_docker(reader, connection)
        )
        try:
            while True:
                stream, data = await asyncio.to_thread(
                    self._read_socket,
                    connection,
                    32768,
                )
                if not data:
                    break
                if stream == 1:
                    writer.write(data)
                    await writer.drain()
                else:
                    logger.debug(
                        "TCP 转发容器错误输出 username=%s exec_id=%s message=%s",
                        username,
                        connection.exec_id,
                        data.decode("utf-8", "replace").strip(),
                    )
        finally:
            input_task.cancel()
            await asyncio.gather(input_task, return_exceptions=True)
            try:
                connection.socket.close()
            except OSError:
                pass
            writer.close()

    @staticmethod
    async def _copy_forward_to_docker(
        reader: Any,
        connection: ExecConnection,
    ) -> None:
        try:
            while True:
                data = await reader.read(32768)
                if not data:
                    return
                await asyncio.to_thread(SSHGateway._write_all, connection.socket, data)
        finally:
            await asyncio.to_thread(SSHGateway._shutdown_write, connection.socket)

    async def open_agent_bridge(
        self,
        username: str,
        agent_path: str,
    ) -> tuple[Any, Path, str]:
        data_root = self.docker.settings.data_root.resolve()
        user_root = (data_root / username).resolve()
        if user_root.parent != data_root:
            raise OSError("用户数据目录无效")
        user_root.mkdir(parents=True, exist_ok=True)
        socket_name = f".docker_proxy_agent_{secrets.token_hex(8)}.sock"
        host_path = user_root / socket_name
        server = await asyncio.start_unix_server(
            partial(self.relay_agent, agent_path),
            path=str(host_path),
        )
        try:
            os.chmod(host_path, 0o600)
        except OSError:
            server.close()
            await server.wait_closed()
            host_path.unlink(missing_ok=True)
            raise
        logger.info("SSH Agent 转发已启用 username=%s", username)
        return server, host_path, f"/data/{socket_name}"

    @staticmethod
    async def relay_agent(
        agent_path: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            agent_reader, agent_writer = await asyncio.open_unix_connection(agent_path)
        except OSError:
            logger.exception("连接 SSH Agent 转发 socket 失败")
            writer.close()
            await writer.wait_closed()
            return
        first = asyncio.create_task(
            SSHGateway.copy_stream(reader, agent_writer)
        )
        second = asyncio.create_task(
            SSHGateway.copy_stream(agent_reader, writer)
        )
        try:
            await asyncio.gather(first, second)
        except (ConnectionError, OSError):
            logger.debug("SSH Agent 转发连接已关闭", exc_info=True)
        finally:
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            agent_writer.close()
            writer.close()
            await asyncio.gather(
                agent_writer.wait_closed(),
                writer.wait_closed(),
                return_exceptions=True,
            )

    @staticmethod
    async def copy_stream(reader: Any, writer: Any) -> None:
        while True:
            data = await reader.read(32768)
            if not data:
                try:
                    writer.write_eof()
                except (AttributeError, OSError):
                    pass
                return
            writer.write(data)
            await writer.drain()

    @staticmethod
    async def close_agent_bridge(
        bridge: tuple[Any, Path, str] | None,
    ) -> None:
        if bridge is None:
            return
        server, host_path, _ = bridge
        server.close()
        await server.wait_closed()
        try:
            host_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("清理 SSH Agent 转发 socket 失败 path=%s", host_path)

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
        if process.subsystem:
            await self._fail(process, f"不支持 SSH 子系统: {process.subsystem}")
            return
        requested = process.command
        if isinstance(requested, bytes):
            requested = requested.decode("utf-8", "replace")
        pid_file = ""
        if requested:
            pid_file = f"/tmp/.docker_proxy_exec_{secrets.token_hex(12)}.pid"
            setup = command_setup(requested)
            command_text = (
                f"printf '%s' $$ > {shlex.quote(pid_file)}; "
                + setup
                + ("exec " if setup else "")
                + requested
            )
            command = ["/bin/sh", "-lc", command_text]
        else:
            command = INTERACTIVE_SHELL
        environment = process_environment(process)
        agent_bridge = None
        agent_path = process.get_agent_path()
        if agent_path:
            try:
                agent_bridge = await self.open_agent_bridge(username, agent_path)
            except OSError as exc:
                logger.exception("创建 SSH Agent 转发失败 username=%s", username)
                await self._fail(process, f"无法建立 SSH Agent 转发: {exc}")
                return
            environment["SSH_AUTH_SOCK"] = agent_bridge[2]
        try:
            connection = await asyncio.to_thread(
                self.docker.open_exec,
                user.container_id,
                command,
                process.term_type,
                process.term_size,
                environment,
            )
        except (DockerException, OSError, ValueError) as exc:
            logger.exception("无法为用户 %s 创建 Docker exec", username)
            await self.close_agent_bridge(agent_bridge)
            await self._fail(process, f"无法进入容器: {exc}")
            return
        logger.info(
            "Docker Exec 已连接 username=%s container_id=%s exec_id=%s command=%s",
            username,
            user.container_id,
            connection.exec_id,
            command,
        )
        input_task = asyncio.create_task(
            self._copy_ssh_to_docker(
                process,
                connection,
                user.container_id,
                pid_file,
            )
        )
        output_task = asyncio.create_task(self._copy_docker_to_ssh(process, connection))
        output_failed = False
        try:
            await output_task
        except Exception:
            output_failed = True
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
            await self.close_agent_bridge(agent_bridge)
        if pid_file:
            try:
                await asyncio.to_thread(
                    self.docker.remove_exec_pid_file,
                    user.container_id,
                    pid_file,
                )
            except DockerException:
                logger.exception(
                    "清理远程命令 PID 文件失败 username=%s path=%s",
                    username,
                    pid_file,
                )
        try:
            exit_code = (
                1
                if output_failed
                else await asyncio.to_thread(
                    self.docker.exec_exit_code,
                    connection,
                )
            )
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
        container_id: str,
        pid_file: str,
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
                except asyncssh.BreakReceived:
                    logger.info(
                        "收到 SSH Break username=%s exec_id=%s",
                        username,
                        connection.exec_id,
                    )
                    if connection.tty or not pid_file:
                        await asyncio.to_thread(
                            self._write_all,
                            connection.socket,
                            b"\x03",
                        )
                    else:
                        await asyncio.to_thread(
                            self.docker.signal_exec,
                            container_id,
                            pid_file,
                            "INT",
                        )
                    continue
                except asyncssh.SignalReceived as exc:
                    signal_name = exc.signal.upper()
                    logger.info(
                        "收到 SSH 信号 username=%s exec_id=%s signal=%s",
                        username,
                        connection.exec_id,
                        signal_name,
                    )
                    control = {
                        "INT": b"\x03",
                        "QUIT": b"\x1c",
                        "TSTP": b"\x1a",
                    }.get(signal_name)
                    if connection.tty and control:
                        await asyncio.to_thread(
                            self._write_all,
                            connection.socket,
                            control,
                        )
                    elif pid_file:
                        await asyncio.to_thread(
                            self.docker.signal_exec,
                            container_id,
                            pid_file,
                            signal_name,
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
                stream, data = await asyncio.to_thread(
                    SSHGateway._read_socket, connection, 32768
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
                output = process.stderr if stream == 2 else process.stdout
                output.write(data)
                await output.drain()
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
    def _read_socket(connection: ExecConnection, size: int) -> tuple[int, bytes]:
        sock = connection.socket
        raw_socket = getattr(sock, "_sock", sock)
        if connection.tty:
            return 1, SSHGateway._read_from_socket(raw_socket, size)
        while True:
            header = SSHGateway._read_exact(raw_socket, 8)
            if not header:
                return 1, b""
            stream = header[0]
            length = int.from_bytes(header[4:8], "big")
            if length:
                return stream, SSHGateway._read_exact(raw_socket, length)

    @staticmethod
    def _read_exact(sock: Any, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = SSHGateway._read_from_socket(sock, size - len(chunks))
            if not chunk:
                if chunks:
                    raise OSError("Docker exec 输出流意外结束")
                return b""
            chunks.extend(chunk)
        return bytes(chunks)

    @staticmethod
    def _read_from_socket(sock: Any, size: int) -> bytes:
        recv = getattr(sock, "recv", None)
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
