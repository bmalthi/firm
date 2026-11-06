"""Local HTTP server lifetime, independent of firm execution and VM lifetime."""

import socket
import threading
import time

import uvicorn

from .errors import FirmError


class LocalServer:
    def __init__(self, app_factory, *, host="127.0.0.1", port=0):
        self.host, self.port = host, port
        self.app_factory = app_factory
        self.server = self.thread = self.socket = None

    def __enter__(self):
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            try:
                self.socket.bind((self.host, self.port))
            except OSError as exc:
                raise FirmError(
                    f"cannot listen on {self.host}:{self.port} ({exc.strerror}); another firm's "
                    "host is probably running. Give this one free ports with --mcp-port and "
                    "--console-port."
                ) from None
            self.port = self.socket.getsockname()[1]
            self.socket.listen(128)
            self.server = uvicorn.Server(
                uvicorn.Config(
                    self.app_factory(self.port),
                    host=self.host,
                    port=self.port,
                    log_level="warning",
                    access_log=False,
                )
            )
            self.thread = threading.Thread(
                target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
            )
            self.thread.start()
            deadline = time.monotonic() + 10
            while not self.server.started:
                if not self.thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("local HTTP server did not become ready")
                time.sleep(0.01)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)
        if self.socket is not None:
            self.socket.close()
