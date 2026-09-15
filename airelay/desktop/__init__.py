"""桌面外壳（托盘 / WebView 窗口）。"""

from .app import DesktopApp, run_desktop
from .tray import TrayController, build_icon_image, tray_available
from .window import find_chromium, open_console, pywebview_available

__all__ = [
    "DesktopApp",
    "TrayController",
    "build_icon_image",
    "find_chromium",
    "open_console",
    "pywebview_available",
    "run_desktop",
    "tray_available",
]
