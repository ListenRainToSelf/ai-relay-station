"""托盘常驻外壳（pystray + Pillow）。

菜单围绕「这个网关现在怎么样」组织：一眼看到运行状态与活跃会话数，
点一下就能开控制台、复制网关地址、翻日志、退出。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable

from ..version import APP_NAME, __version__

log = logging.getLogger(__name__)


def tray_available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401, F401
    except Exception:
        return False
    if sys.platform.startswith("linux"):
        # 无 X/Wayland 显示时托盘无法工作（NAS 场景）
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def build_icon_image(size: int = 64):
    """画一个「双向箭头」图标：表示请求与响应的中转。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(2, size // 16)
    radius = size // 4
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        fill=(13, 20, 33, 255),
        outline=(48, 130, 246, 255),
        width=max(1, size // 32),
    )

    unit = size / 64.0
    bar = max(2, int(3 * unit))
    head = max(3, int(7 * unit))
    left = int(15 * unit)
    right = int(49 * unit)

    # 上行箭头（青色，指向右）
    top_y = int(24 * unit)
    draw.line([(left, top_y), (right - head, top_y)], fill=(56, 189, 248, 255), width=bar)
    draw.polygon(
        [(right, top_y), (right - head, top_y - head), (right - head, top_y + head)],
        fill=(56, 189, 248, 255),
    )

    # 下行箭头（绿色，指向左）
    bottom_y = int(40 * unit)
    draw.line([(right, bottom_y), (left + head, bottom_y)], fill=(52, 211, 153, 255), width=bar)
    draw.polygon(
        [(left, bottom_y), (left + head, bottom_y - head), (left + head, bottom_y + head)],
        fill=(52, 211, 153, 255),
    )
    return image


def copy_to_clipboard(text: str) -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            subprocess.run("clip", input=text.encode("utf-16le"), shell=True, check=True)
            return True
        if sys.platform == "darwin":
            subprocess.run("pbcopy", input=text.encode("utf-8"), check=True)
            return True
        for tool, args in (("wl-copy", []), ("xclip", ["-selection", "clipboard"]), ("xsel", ["-ib"])):
            if shutil.which(tool):
                subprocess.run([tool, *args], input=text.encode("utf-8"), check=True)
                return True
    except Exception:
        log.debug("复制到剪贴板失败", exc_info=True)
    return False


def open_folder(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        log.warning("无法打开目录 %s", path, exc_info=True)


class TrayController:
    def __init__(
        self,
        ctx: Any,
        host: Any,
        *,
        on_open_console: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.ctx = ctx
        self.host = host
        self.on_open_console = on_open_console
        self.on_quit = on_quit
        self._icon: Any = None
        self._last_message = ""

    # ------------------------------------------------------------------ 生命周期
    def run(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("打开控制台", self._open_console, default=True),
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("复制网关地址", self._copy_address),
            pystray.MenuItem("复制 OpenAI 基地址", self._copy_openai_address),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开数据目录", self._open_data_dir),
            pystray.MenuItem("打开日志文件", self._open_log),
            pystray.MenuItem("重新读取设置", self._reload_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        )
        self._icon = pystray.Icon(
            "airelay",
            icon=build_icon_image(),
            title=f"{APP_NAME} v{__version__}",
            menu=menu,
        )
        log.info("托盘图标已就绪：右键可打开控制台 / 复制地址 / 查看日志 / 退出")
        try:
            self._icon.run()
        except Exception:
            log.exception("托盘启动失败，转为前台运行")
            raise
        log.info("托盘已退出")

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def notify(self, message: str, title: str = APP_NAME) -> None:
        self._last_message = message
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:
                log.debug("托盘通知失败", exc_info=True)

    # ------------------------------------------------------------------ 菜单动作
    def _status_text(self, _item: Any = None) -> str:
        if not self.host.running:
            return f"状态：未运行 · {self.ctx.base_url()}"
        active = self.ctx.live.active_count
        mode = "桌面常驻" if self.ctx.mode == "desktop" else "后台服务"
        return f"状态：{mode} · 活跃会话 {active}"

    def _open_console(self, _icon: Any = None, _item: Any = None) -> None:
        self.on_open_console()

    def _copy_address(self, _icon: Any = None, _item: Any = None) -> None:
        address = self.ctx.base_url()
        if copy_to_clipboard(address):
            self.notify(f"已复制：{address}")
        else:
            self.notify(f"网关地址：{address}（剪贴板不可用，请手动复制）")

    def _copy_openai_address(self, _icon: Any = None, _item: Any = None) -> None:
        address = self.ctx.openai_base_url()
        if copy_to_clipboard(address):
            self.notify(f"已复制 OpenAI 基地址：{address}")
        else:
            self.notify(f"OpenAI 基地址：{address}")

    def _open_data_dir(self, _icon: Any = None, _item: Any = None) -> None:
        open_folder(str(self.ctx.paths.data_dir))

    def _open_log(self, _icon: Any = None, _item: Any = None) -> None:
        log_file = self.ctx.paths.log_dir / "airelay.log"
        if log_file.is_file():
            if sys.platform == "win32":
                os.startfile(str(log_file))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(log_file)])
        else:
            open_folder(str(self.ctx.paths.log_dir))

    def _reload_settings(self, _icon: Any = None, _item: Any = None) -> None:
        def worker() -> None:
            import asyncio

            async def reload() -> None:
                if self.ctx.session_factory is None:
                    return
                async with self.ctx.session_factory() as session:
                    await self.ctx.settings.load(session)
                    await self.ctx.mapping.refresh(session)

            try:
                asyncio.run(reload())
                self.notify("设置与模型别名已重新加载")
            except Exception as exc:
                self.notify(f"重新加载失败：{exc}")

        threading.Thread(target=worker, name="airelay-reload", daemon=True).start()

    def _quit(self, icon: Any = None, _item: Any = None) -> None:
        log.info("用户从托盘退出")
        self.stop()
        self.on_quit()
