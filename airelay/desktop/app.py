"""桌面常驻外壳：HTTP 宿主（后台线程）+ 托盘（主线程）+ 控制台窗口。

Windows 上托盘必须跑在主线程，所以窗口用独立子进程承载
（`python -m airelay.desktop.window`），三者互不阻塞。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

from ..context import AppContext, MODE_DESKTOP
from ..host import AppHost
from .tray import TrayController, tray_available
from .window import open_console

log = logging.getLogger(__name__)


class DesktopApp:
    def __init__(
        self,
        ctx: AppContext,
        *,
        host: str | None = None,
        port: int | None = None,
        open_window: bool = True,
        use_tray: bool = True,
    ) -> None:
        self.ctx = ctx
        self.ctx.mode = MODE_DESKTOP
        self.host = AppHost(ctx, host=host, port=port)
        self.open_window = open_window
        self.use_tray = use_tray
        self._tray: TrayController | None = None
        self._window_process: subprocess.Popen | None = None
        self._quit_event = threading.Event()

    # ------------------------------------------------------------------ 运行
    def run(self) -> int:
        if not self.host.start(wait=True, timeout=25):
            message = self.host.bind_error or (
                "服务启动超时（25s 内没起来）。宿主线程的异常已写入日志，"
                "可查看数据目录 logs/airelay.log 里的「HTTP 宿主线程异常退出」"
            )
            log.error("%s", message)
            self._fallback_headless(message)
            return 1

        url = f"{self.ctx.base_url()}/admin"
        log.info("控制台地址：%s", url)

        if self.open_window:
            self.open_console_window()

        if self.use_tray and tray_available():
            try:
                self._run_tray(url)
                return 0
            except Exception:
                log.exception("托盘不可用，改为前台常驻（Ctrl+C 退出）")
        else:
            if self.use_tray:
                log.warning("当前环境没有图形界面，托盘不可用（NAS / 纯命令行场景属正常）")

        # 没有托盘：窗口独立进程活着就继续常驻，否则直接前台阻塞
        try:
            while not self._quit_event.is_set():
                time.sleep(0.5)
                if self._window_process is not None and self._window_process.poll() is not None:
                    break
        except KeyboardInterrupt:
            pass
        self.shutdown()
        return 0

    def _run_tray(self, url: str) -> None:
        self._tray = TrayController(
            self.ctx,
            self.host,
            on_open_console=self.open_console_window,
            on_quit=self._on_quit,
        )
        self._tray.notify(f"网关已在后台运行：{self.ctx.openai_base_url()}")
        self._tray.run()
        self.shutdown()

    def _on_quit(self) -> None:
        self._quit_event.set()
        self.shutdown()

    # ------------------------------------------------------------------ 窗口
    def open_console_window(self) -> None:
        url = f"{self.ctx.base_url()}/admin"
        profile = self.ctx.paths.data_dir / "webview"
        # 已有窗口进程活着就复用（避免开出多个窗口）
        if self._window_process is not None and self._window_process.poll() is None:
            self._focus_existing()
            return
        try:
            self._window_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "airelay.desktop.window",
                    "--url",
                    url,
                    "--profile-dir",
                    str(profile),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("已打开控制台窗口：%s", url)
            return
        except Exception:
            log.warning("子进程开窗失败，改用本进程方式打开", exc_info=True)
        mode = open_console(url, profile_dir=profile)
        log.info("控制台已打开（%s）：%s", mode, url)

    def _focus_existing(self) -> None:
        log.info("控制台窗口已在运行，已复用：%s", self.ctx.base_url())

    # ------------------------------------------------------------------ 收尾
    def shutdown(self) -> None:
        if self._tray is not None:
            self._tray.stop()
        self.host.stop()

    def _fallback_headless(self, reason: str) -> None:
        log.error("%s", reason)
        log.error(
            "网关未能启动。若是端口被占用，请在控制台设置里换端口，"
            "或用 `python -m airelay --port 8090` 指定其它端口后重试。"
        )


def run_desktop(ctx: AppContext, *, open_window: bool = True, use_tray: bool = True) -> int:
    return DesktopApp(ctx, open_window=open_window, use_tray=use_tray).run()
