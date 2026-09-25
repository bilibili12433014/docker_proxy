from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from werkzeug.serving import BaseWSGIServer, make_server

from .config import Settings
from .config_store import ConfigStore
from .docker_service import DockerService
from .keys import KeyManager
from .ssh_gateway import SSHGateway
from .web import create_app

logger = logging.getLogger(__name__)


class WebServer:
    def __init__(self, host: str, port: int, app: object):
        self.server: BaseWSGIServer = make_server(host, port, app, threaded=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="docker-proxy-web", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


async def run(settings: Settings) -> None:
    store = ConfigStore(settings.config_path)
    key_manager = KeyManager(settings.key_dir)
    docker_service = DockerService(settings)
    app = create_app(settings, store, docker_service, key_manager)
    web_server = WebServer(settings.listen_host, settings.web_port, app)
    ssh_gateway = SSHGateway(store, docker_service, key_manager)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals = []

    def request_shutdown(received_signal: signal.Signals) -> None:
        if not stop_event.is_set():
            logger.info("收到退出信号 %s，开始中断全部连接", received_signal.name)
            stop_event.set()

    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                shutdown_signal,
                request_shutdown,
                shutdown_signal,
            )
            registered_signals.append(shutdown_signal)
        except (NotImplementedError, RuntimeError):
            break
    web_server.start()
    try:
        await ssh_gateway.start(settings.listen_host, settings.ssh_port)
        logger.info(
            "Docker Proxy 已启动，管理端口=%s:%d SSH端口=%s:%d",
            settings.listen_host,
            settings.web_port,
            settings.listen_host,
            settings.ssh_port,
        )
        logger.debug(
            "运行路径 config=%s key_dir=%s data_root=%s",
            settings.config_path,
            settings.key_dir,
            settings.data_root,
        )
        await stop_event.wait()
    finally:
        for shutdown_signal in registered_signals:
            loop.remove_signal_handler(shutdown_signal)
        logger.info("正在停止 Docker Proxy")
        await asyncio.gather(
            ssh_gateway.close(),
            asyncio.to_thread(web_server.close),
        )
        logger.info("Docker Proxy 已完全停止")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config_path = Path(os.environ.get("DOCKER_PROXY_CONFIG", "config.yaml"))
    try:
        settings = (
            Settings.from_file(config_path)
            if config_path.is_file()
            else Settings.bootstrap(config_path)
        )
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("服务已停止")
    except (TypeError, ValueError) as exc:
        logger.error("配置错误: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
