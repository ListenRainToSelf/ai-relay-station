"""时间工具：全库统一使用「无时区的 UTC」存储，对外序列化时补 Z 后缀。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    """当前 UTC 时间（naive，去掉 tzinfo 便于 SQLite 存取）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def to_iso_ms(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime(ISO_FORMAT)


def parse_iso(text: str | None) -> datetime | None:
    """解析 ISO8601 字符串，兼容带 Z / 带偏移 / 只有日期三种写法。"""
    if not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(text.strip()[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def bucket_start(value: datetime, minutes: int = 1) -> datetime:
    """向下取整到分钟桶（方案 7.2：明细 + 分钟级预聚合相互印证）。"""
    minute = value.minute - (value.minute % minutes)
    return value.replace(minute=minute, second=0, microsecond=0)


def humanize_seconds(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{int(rest)}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h{int(minutes)}m"


def days_ago(days: float) -> datetime:
    return utcnow() - timedelta(days=days)
