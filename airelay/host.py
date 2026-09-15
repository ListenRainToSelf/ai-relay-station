"""进程内 HTTP 宿主：在后台线程里跑 uvicorn，并支持监听参数热重绑定。

为什么不用 `uvicorn.run()` 直接起：方案 11 节要求「服务 IP 与端口一经修改
立即生效，不重启进程」。做法是把 uvicorn 跑在受控线程里，改端口时优雅
停掉旧 server、在新端口起一个新的（新的生命周期会重建引擎与连接池）。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any

import uvicorn

from .context import AppContext
from .main import create_app

log = logging.getLogger(__name__)


class AppHost:
    def __init__(self, ctx: AppContext, *, host: str | None = None, port: int | None = None) -> None:
        self.ctx = ctx
        self._host = host or ctx.host
        self._port = port or ctx.port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rebind = threading.Event()
        self._bind_error: str = ""
        self._started = threading.Event()

    # ------------------------------------------------------------------ 属性
    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def bind_error(self) -> str:
        return self._bind_error

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ 控制
    def start(self, *, wait: bool = True, timeout: float = 20.0) -> bool:
        if self.running:
            return True
        self._stop.clear()
        self._rebind.clear()
        self._bind_error = ""
        self._started.clear()
        self.ctx.restart_callback = self.async_rebind
        self._thread = threading.Thread(target=self._supervise, name="airelay-http", daemon=True)
        self._thread.start()
        if not wait:
            return True
        return self._started.wait(timeout) and not self._bind_error

    def stop(self, *, join_timeout: float = 15.0) -> None:
        self._stop.set()
        self._rebind.clear()
        if self._server is not None:
            self._server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        self._thread = None

    async def async_rebind(self) -> None:
        """供 AppContext 在设置变更时调用（在服务的事件循环里执行，必须立刻返回）。"""
        target_host, target_port = self.ctx.host, self.ctx.port
        if target_host == self._host and target_port == self._port:
            return
        self._host, self._port = target_host, target_port
        self._rebind.set()
        if self._server is not None:
            # 优雅退出：正在处理的请求（包括触发这次变更的那个）会先跑完
            self._server.should_exit = True
            log.info("正在切换到新的监听地址 %s:%s", self._host, self._port)

    # ------------------------------------------------------------------ 线程体
    def _supervise(self) -> None:
        while not self._stop.is_set():
            if not self._port_available():
                self._bind_error = f"端口 {self._port} 已被占用"
                log.error("%s，服务未能启动", self._bind_error)
                self._started.set()
                return
            app = create_app(self.ctx)
            config = uvicorn.Config(
                app,
                host=self._host,
                port=self._port,
                log_level="warning",
                access_log=False,
                loop="asyncio",
                lifespan="on",
                ws="auto",
                timeout_graceful_shutdown=15,
                server_header=False,
                date_header=True,
            )
            server = uvicorn.Server(config)
            self._server = server
            self._bind_error = ""
            self._started.set()
            log.info("HTTP 服务已就绪：http://%s:%s", self._host, self._port)
            try:
                server.run()
            except SystemExit as exc:  # 端口占用等启动失败
                self._bind_error = f"服务启动失败（端口 {self._port} 可能被占用）：{exc}"
                log.error(self._bind_error)
                self._server = None
                return
            except Exception:
                log.exception("HTTP 服务异常退出")
                self._server = None
                if not self._rebind.is_set():
                    return
            self._server = None
            if self._rebind.is_set():
                self._rebind.clear()
                # 给旧端口一个释放窗口，避免 TIME_WAIT 导致重绑定失败
                time.sleep(0.4)
                continue
            break

    def _port_available(self) -> bool:
        family = socket.AF_INET6 if ":" in self._host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((self._host, self._port))
                return True
            except OSError:
                return False


def run_blocking(ctx: AppContext, *, host: str | None = None, port: int | None = None) -> int:
    """前台阻塞运行（容器 / systemd / 无头模式用这个）。"""
    host = host or ctx.host
    port = port or ctx.port
    app = create_app(ctx)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=ctx.settings.get_str("logs.level", "INFO").lower(),
        access_log=False,
        lifespan="on",
        ws="auto",
        timeout_graceful_shutdown=15,
        server_header=False,
    )
    server = uvicorn.Server(config)

    # 让设置里的热重绑定在阻塞模式下也给出明确提示
    async def _unsupported_rebind() -> None:
        ctx.pending_restart = True
        log.warning("当前以前台模式运行，IP/端口变更需要重启进程后才能生效")

    ctx.restart_callback = _unsupported_rebind
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("收到中断信号，正在退出")
    return 0


def describe_listeners(ctx: AppContext) -> dict[str, Any]:
    return {"host": ctx.host, "port": ctx.port, "base_url": ctx.base_url()}
