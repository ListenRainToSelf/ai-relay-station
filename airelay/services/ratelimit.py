"""限流：进程内滑动窗口计数（RPM / TPM）。

单机场景不需要 Redis，内存窗口即可；计数分「全局」与「每 Key」两层，
对应方案 11 节设置表里的「限流」分组。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

WINDOW_SECONDS = 60.0


class SlidingWindowCounter:
    """按名字维护滑动窗口累计值（时间戳 + 权重）。"""

    def __init__(self, window: float = WINDOW_SECONDS, max_names: int = 4096) -> None:
        self.window = window
        self.max_names = max_names
        self._events: dict[str, deque[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def _prune(self, events: deque[tuple[float, float]], now: float) -> None:
        cutoff = now - self.window
        while events and events[0][0] < cutoff:
            events.popleft()

    def total(self, name: str, *, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        with self._lock:
            events = self._events.get(name)
            if not events:
                return 0.0
            self._prune(events, now)
            return sum(weight for _, weight in events)

    def peek(self, name: str, adding: float = 1.0, *, now: float | None = None) -> tuple[float, float]:
        """返回 (当前窗口累计, 最早事件剩余存活秒数)。"""
        now = now if now is not None else time.monotonic()
        with self._lock:
            events = self._events.get(name)
            if not events:
                return 0.0, 0.0
            self._prune(events, now)
            if not events:
                return 0.0, 0.0
            return sum(w for _, w in events), max(0.0, self.window - (now - events[0][0]))

    def add(self, name: str, weight: float = 1.0, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            events = self._events.setdefault(name, deque())
            self._prune(events, now)
            events.append((now, weight))
            if len(self._events) > self.max_names:
                self._evict_stale(now)

    def reset(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._events.clear()
            else:
                self._events.pop(name, None)

    def _evict_stale(self, now: float) -> None:
        for key in list(self._events.keys()):
            events = self._events.get(key)
            if events is not None:
                self._prune(events, now)
                if not events:
                    self._events.pop(key, None)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._events.keys())


class RateLimiter:
    """RPM / TPM 双层限流器。"""

    def __init__(self) -> None:
        self.requests = SlidingWindowCounter()
        self.tokens = SlidingWindowCounter()

    # ---------------------------------------------------------------- 检查
    def check_request(
        self, *, key_id: str, key_rpm: int, global_rpm: int
    ) -> dict[str, Any] | None:
        """返回 None 表示放行；否则返回违规详情（供上层构造 429）。"""
        if global_rpm > 0:
            used = self.requests.total("global")
            if used + 1 > global_rpm:
                return {
                    "scope": "global",
                    "limit": global_rpm,
                    "used": int(used),
                    "metric": "rpm",
                    "retry_after": int(self.requests.peek("global")[1]) or 1,
                }
        if key_rpm > 0 and key_id:
            used = self.requests.total(f"key:{key_id}")
            if used + 1 > key_rpm:
                return {
                    "scope": "key",
                    "limit": key_rpm,
                    "used": int(used),
                    "metric": "rpm",
                    "retry_after": int(self.requests.peek(f"key:{key_id}")[1]) or 1,
                }
        return None

    def check_tokens(self, *, key_id: str, estimated: int, key_tpm: int, global_tpm: int) -> dict[str, Any] | None:
        if global_tpm > 0:
            used = self.tokens.total("global")
            if used + estimated > global_tpm:
                return {
                    "scope": "global",
                    "limit": global_tpm,
                    "used": int(used),
                    "metric": "tpm",
                    "retry_after": int(self.tokens.peek("global")[1]) or 1,
                }
        if key_tpm > 0 and key_id:
            used = self.tokens.total(f"key:{key_id}")
            if used + estimated > key_tpm:
                return {
                    "scope": "key",
                    "limit": key_tpm,
                    "used": int(used),
                    "metric": "tpm",
                    "retry_after": int(self.tokens.peek(f"key:{key_id}")[1]) or 1,
                }
        return None

    # ---------------------------------------------------------------- 记账
    def charge_request(self, key_id: str) -> None:
        self.requests.add("global")
        if key_id:
            self.requests.add(f"key:{key_id}")

    def charge_tokens(self, key_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        self.tokens.add("global", tokens)
        if key_id:
            self.tokens.add(f"key:{key_id}", tokens)

    # ---------------------------------------------------------------- 状态
    def usage_snapshot(self, key_ids: list[str] | None = None) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "global": {
                "rpm_used": int(self.requests.total("global")),
                "tpm_used": int(self.tokens.total("global")),
            },
            "keys": {},
        }
        for key_id in key_ids or []:
            snapshot["keys"][key_id] = {
                "rpm_used": int(self.requests.total(f"key:{key_id}")),
                "tpm_used": int(self.tokens.total(f"key:{key_id}")),
            }
        return snapshot

    def reset(self, key_id: str | None = None) -> None:
        self.requests.reset(f"key:{key_id}" if key_id else None)
        self.tokens.reset(f"key:{key_id}" if key_id else None)
