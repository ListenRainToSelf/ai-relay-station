"""日志初始化：控制台 + 数据目录下的滚动文件。

NAS 场景下把日志落到数据目录便于 `tail -f` 排查；Windows 托盘场景下
控制台可能不可见，所以文件日志是主要排查入口。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_CONFIGURED = False
_HEADLESS = False


def ensure_console_streams() -> bool:
    """给「没有控制台」的进程兜底标准流，返回是否处于无控制台状态。

    用 pythonw（托盘静默启动）拉起时 ``sys.stdout`` / ``sys.stderr`` 都是 None，
    而不少库假设它们存在——例如 uvicorn 的日志格式化器会调 ``sys.stdout.isatty()``，
    直接抛 AttributeError 把宿主线程搞死（现象是「服务启动超时」且看不出原因）。
    这里把它们接到 os.devnull：语义上「能写但看不见」，日志另有文件 handler。
    """
    global _HEADLESS
    _HEADLESS = getattr(sys, "stdout", None) is None
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8", buffering=1))
            except OSError:  # pragma: no cover - 极端环境
                pass
    if getattr(sys, "stdin", None) is None:
        try:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
        except OSError:  # pragma: no cover
            pass
    return _HEADLESS


def is_headless() -> bool:
    return _HEADLESS

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _ColorFormatter(logging.Formatter):
    _COLORS = {
        logging.DEBUG: "\033[38;5;244m",
        logging.INFO: "\033[38;5;39m",
        logging.WARNING: "\033[38;5;214m",
        logging.ERROR: "\033[38;5;203m",
        logging.CRITICAL: "\033[48;5;203;38;5;231m",
    }
    _RESET = "\033[0m"

    def __init__(self, *args, use_color: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = self._COLORS.get(record.levelno)
        return f"{color}{text}{self._RESET}" if color else text


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """初始化根 logger。重复调用只调整级别，不重复添加 handler。"""
    global _CONFIGURED
    root = logging.getLogger()
    numeric = _coerce_level(level)

    if _CONFIGURED:
        root.setLevel(numeric)
        for handler in root.handlers:
            handler.setLevel(numeric)
        return

    root.setLevel(numeric)
    root.handlers.clear()

    # pythonw（无控制台）下 stdout/stderr 都是 None，此时只有文件日志
    stream = sys.stdout or sys.stderr
    if stream is not None:
        console = logging.StreamHandler(stream)
        console.setLevel(numeric)
        use_color = bool(getattr(stream, "isatty", lambda: False)()) and sys.platform != "win32"
        console.setFormatter(_ColorFormatter(_FORMAT, _DATEFMT, use_color=use_color))
        root.addHandler(console)

    if log_dir is not None:
        try:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "airelay.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(numeric)
            file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
            root.addHandler(file_handler)
        except OSError:  # 只读挂载等极端情况：退化为仅控制台
            root.warning("无法写入日志文件目录 %s，仅使用控制台日志", log_dir)

    # 降噪：uvicorn 的 access 日志由我们自己记录更详细的版本
    logging.getLogger("uvicorn.access").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def quieten_uvicorn_access() -> None:
    logging.getLogger("uvicorn.access").disabled = True
