"""路径解析：数据目录、数据库、静态前端、日志目录。

NAS / 服务器场景下用 `--data-dir` 或 `AIRELAY_DATA_DIR` 指定即可；
Windows 桌面场景默认落在 %LOCALAPPDATA%\\airelay。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .version import APP_SLUG

ENV_DATA_DIR = "AIRELAY_DATA_DIR"

PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGE_WEB_DIR = PACKAGE_DIR / "web"
PROJECT_ROOT = PACKAGE_DIR.parent
PROJECT_WEB_DIR = PROJECT_ROOT / "web"


def default_data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_SLUG
        return Path.home() / f".{APP_SLUG}"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_SLUG
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_SLUG
    return Path.home() / ".local" / "share" / APP_SLUG


def find_web_dir() -> Path | None:
    """静态前端目录：优先包内，其次项目根（源码直接运行时）。"""
    for candidate in (PACKAGE_WEB_DIR, PROJECT_WEB_DIR):
        if (candidate / "index.html").is_file():
            return candidate
    return None


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    db_path: Path
    log_dir: Path
    web_dir: Path | None
    desktop_state_path: Path

    @classmethod
    def build(cls, data_dir: Path | str | None = None) -> "AppPaths":
        base = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
        base.mkdir(parents=True, exist_ok=True)
        return cls(
            data_dir=base,
            db_path=base / "airelay.db",
            log_dir=base / "logs",
            web_dir=find_web_dir(),
            desktop_state_path=base / "desktop.json",
        )
