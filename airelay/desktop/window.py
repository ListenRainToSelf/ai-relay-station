"""控制台窗口：优先 pywebview 内嵌窗口，其次 Chromium 系应用的「独立窗口」模式。

之所以不把 pywebview 作为硬依赖：NAS/Linux 服务端根本不需要 GUI，
Windows 上也常常没装 pythonnet。这里做成三级降级，任何环境都能打开控制台。
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

WINDOW_TITLE = "本地 AI 中转站 · 控制台"

# Chromium 系浏览器的常见可执行文件名（用 --app= 起独立窗口）
_CHROMIUM_CANDIDATES = [
    "msedge",
    "chrome",
    "chromium",
    "chromium-browser",
    "brave",
    "brave-browser",
    "google-chrome",
    "google-chrome-stable",
    "microsoft-edge",
    "microsoft-edge-stable",
]

_WINDOWS_PATHS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
]


def pywebview_available() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def find_chromium() -> str | None:
    for name in _CHROMIUM_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        for pattern in _WINDOWS_PATHS:
            expanded = os.path.expandvars(pattern)
            if os.path.isfile(expanded):
                return expanded
    elif sys.platform == "darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ):
            if os.path.isfile(path):
                return path
    return None


def open_console(
    url: str,
    *,
    profile_dir: Path | None = None,
    width: int = 1360,
    height: int = 900,
    prefer: str = "auto",
    blocking: bool = False,
) -> str:
    """打开控制台窗口，返回实际使用的方式（pywebview / chromium / browser）。"""
    if prefer in ("auto", "pywebview") and pywebview_available():
        try:
            return _open_pywebview(url, width=width, height=height, blocking=blocking)
        except Exception:
            log.warning("pywebview 启动失败，改用浏览器独立窗口", exc_info=True)
            if prefer == "pywebview":
                raise

    chromium = find_chromium() if prefer in ("auto", "chromium") else None
    if chromium:
        _open_chromium(chromium, url, profile_dir=profile_dir, width=width, height=height)
        return "chromium"

    webbrowser.open(url)
    return "browser"


def _open_pywebview(url: str, *, width: int, height: int, blocking: bool) -> str:
    import webview

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=width,
        height=height,
        min_size=(900, 620),
        text_select=True,
    )
    if blocking:
        webview.start()
    else:
        import threading

        threading.Thread(target=webview.start, name="airelay-webview", daemon=True).start()
    return "pywebview"


def _open_chromium(
    executable: str, url: str, *, profile_dir: Path | None, width: int, height: int
) -> None:
    args = [
        executable,
        f"--app={url}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]
    if profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        args.append(f"--user-data-dir={profile_dir}")
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


# --------------------------------------------------------------------------- #
# 独立进程入口：`python -m airelay.desktop.window --url http://127.0.0.1:8000/admin`
# 桌面托盘用它把 WebView 窗口放进自己的进程，规避 GUI 主线程限制。
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打开本地 AI 中转站控制台窗口")
    parser.add_argument("--url", required=True)
    parser.add_argument("--profile-dir", default="")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--prefer", default="auto", choices=("auto", "pywebview", "chromium", "browser"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    profile = Path(args.profile_dir).expanduser() if args.profile_dir else None
    # 独立进程里让 pywebview 阻塞运行，窗口关掉即进程退出
    mode = open_console(
        args.url,
        profile_dir=profile,
        width=args.width,
        height=args.height,
        prefer=args.prefer,
        blocking=True,
    )
    log.info("控制台窗口已打开（%s）：%s", mode, args.url)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
