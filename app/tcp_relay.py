from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("输出连接已关闭")
        view = view[written:]


def upload(sock: socket.socket) -> None:
    try:
        while True:
            data = os.read(sys.stdin.fileno(), 65536)
            if not data:
                break
            sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def enter_network_namespace(pid: int) -> None:
    namespace = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)
    try:
        clone_newnet = getattr(os, "CLONE_NEWNET", 0x40000000)
        setns = getattr(os, "setns", None)
        if setns is not None:
            setns(namespace, clone_newnet)
            return
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.setns(namespace, clone_newnet) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    finally:
        os.close(namespace)


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    try:
        enter_network_namespace(int(sys.argv[1]))
        sock = socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=10)
        sock.settimeout(None)
    except (OSError, ValueError) as exc:
        print(f"CONNECT_ERROR: {exc}", file=sys.stderr, flush=True)
        return 111
    print("CONNECTED", file=sys.stderr, flush=True)
    threading.Thread(target=upload, args=(sock,), daemon=True).start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            write_all(sys.stdout.fileno(), data)
    except OSError as exc:
        print(f"RELAY_ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
