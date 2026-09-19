"""实时会话监控：进程内活跃请求注册表 + 增量推送。

方案 8 节的落地：活跃态只进内存不落库，历史统计走 usage_logs；
变化通过 WebSocket 推给控制台，同一时刻只展示「进行中」的会话。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import ErrorCode
from ..timeutil import humanize_seconds, to_iso, utcnow

log = logging.getLogger(__name__)

RECENT_CAPACITY = 200

# tick 帧的最小间隔：流式每个块都会触发一次状态变化，不限流就变成刷屏
TICK_MIN_INTERVAL_SECONDS = 0.2


@dataclass
class LiveSession:
    request_id: str
    key_id: str = ""
    key_name: str = ""
    key_prefix: str = ""
    channel_id: str = ""
    channel_name: str = ""
    provider_type: str = ""
    model: str = ""
    upstream_model: str = ""
    client_ip: str = ""
    stream: bool = False
    attempts: int = 1
    status: str = "running"  # running | ok | error | stalled | abandoned
    error_code: str = ""
    finish_reason: str = ""
    started_at: Any = field(default_factory=utcnow)
    first_token_at: Any = None
    last_activity_at: Any = field(default_factory=utcnow)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_source: str = "estimated"
    chars: int = 0
    chunks: int = 0
    cost_units: int = 0
    # 非对话能力（语音合成/识别、图片生成）的计量：张数 / 字符数 / 秒数
    units: int = 0
    unit_kind: str = ""
    ended_at: Any = None

    # ---------------------------------------------------------------- 派生
    @property
    def elapsed_ms(self) -> float:
        end = self.ended_at or utcnow()
        return max(0.0, (end - self.started_at).total_seconds() * 1000.0)

    @property
    def first_token_ms(self) -> float:
        if self.first_token_at is None:
            return 0.0
        return max(0.0, (self.first_token_at - self.started_at).total_seconds() * 1000.0)

    @property
    def speed_tok_s(self) -> float:
        """输出速度：优先用「首字之后」的时长，避免把首字节等待算进速率。"""
        base = self.first_token_at or self.started_at
        end = self.ended_at or utcnow()
        seconds = max((end - base).total_seconds(), 0.001)
        if self.completion_tokens <= 0:
            return 0.0
        return round(self.completion_tokens / seconds, 2)

    def is_stalled(self, stale_seconds: float) -> bool:
        if self.status != "running":
            return False
        idle = (utcnow() - self.last_activity_at).total_seconds()
        return idle >= stale_seconds

    def to_dict(self, *, stale_seconds: float = 300.0, recent: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data.pop("started_at", None)
        data.pop("first_token_at", None)
        data.pop("last_activity_at", None)
        data.pop("ended_at", None)
        data["started_at"] = to_iso(self.started_at)
        data["elapsed_ms"] = round(self.elapsed_ms, 1)
        data["elapsed_text"] = humanize_seconds(self.elapsed_ms / 1000.0)
        data["first_token_ms"] = round(self.first_token_ms, 1)
        data["speed_tok_s"] = self.speed_tok_s
        data["stalled"] = self.is_stalled(stale_seconds)
        if data["stalled"]:
            data["status"] = "stalled"
        data["recent"] = recent
        data["idle_seconds"] = round((utcnow() - self.last_activity_at).total_seconds(), 1)
        return data


class LiveRegistry:
    """活跃会话表 + 最近完成请求环形缓冲 + 订阅广播。"""

    def __init__(self) -> None:
        self._active: dict[str, LiveSession] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT_CAPACITY)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._revision = 0
        self._last_tick_at = 0.0
        self._total_started = 0
        self._total_finished = 0
        self._total_errors = 0
        self._total_tokens = 0
        self._peak_active = 0

    # ---------------------------------------------------------------- 生命周期
    def start(self, session: LiveSession) -> LiveSession:
        self._active[session.request_id] = session
        self._total_started += 1
        self._peak_active = max(self._peak_active, len(self._active))
        self._touch()
        return session

    def get(self, request_id: str) -> LiveSession | None:
        return self._active.get(request_id)

    def note_activity(self, request_id: str, *, chars: int = 0, chunks: int = 1) -> None:
        session = self._active.get(request_id)
        if session is None:
            return
        now = utcnow()
        if session.first_token_at is None and (chars > 0 or chunks > 0):
            session.first_token_at = now
        session.last_activity_at = now
        session.chars += max(0, chars)
        session.chunks += max(0, chunks)
        self._touch()

    def update_usage(
        self,
        request_id: str,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        source: str | None = None,
    ) -> None:
        session = self._active.get(request_id)
        if session is None:
            return
        if prompt_tokens is not None and prompt_tokens >= 0:
            session.prompt_tokens = prompt_tokens
        if completion_tokens is not None and completion_tokens >= 0:
            session.completion_tokens = completion_tokens
        if total_tokens is not None and total_tokens >= 0:
            session.total_tokens = total_tokens
        elif session.prompt_tokens or session.completion_tokens:
            session.total_tokens = session.prompt_tokens + session.completion_tokens
        if source:
            session.usage_source = source
        self._touch()

    def set_units(self, request_id: str, *, units: int, unit_kind: str) -> None:
        session = self._active.get(request_id)
        if session is None:
            return
        session.units = int(units or 0)
        session.unit_kind = unit_kind or ""
        self._touch()

    def set_channel(
        self, request_id: str, *, channel_id: str, channel_name: str, provider_type: str, upstream_model: str
    ) -> None:
        session = self._active.get(request_id)
        if session is None:
            return
        session.channel_id = channel_id
        session.channel_name = channel_name
        session.provider_type = provider_type
        session.upstream_model = upstream_model
        self._touch()

    def finish(
        self,
        request_id: str,
        *,
        status: str = "ok",
        error_code: str = "",
        finish_reason: str = "",
        cost_units: int = 0,
        retention: int = 0,
    ) -> LiveSession | None:
        session = self._active.pop(request_id, None)
        if session is None:
            return None
        session.status = status
        session.error_code = error_code
        session.finish_reason = finish_reason
        session.cost_units = cost_units
        session.ended_at = utcnow()
        self._total_finished += 1
        if status == "error":
            self._total_errors += 1
        self._total_tokens += session.total_tokens
        keep = 20 if retention <= 0 else min(max(retention, 10), RECENT_CAPACITY)
        self._recent.appendleft(session.to_dict(recent=True))
        while len(self._recent) > keep:
            self._recent.pop()
        self._touch()
        return session

    # ---------------------------------------------------------------- 兜底清理
    def sweep_stale(
        self,
        *,
        idle_seconds: float,
        max_age_seconds: float,
        retention: int = 0,
    ) -> list[str]:
        """回收僵尸会话。

        正常路径下请求结束会调 finish()；但如果响应构造失败、客户端半途消失
        或生成器被取消而没有走到收尾，会话会永远挂在活跃表里。这里按
        「静默时长 + 总时长」双阈值兜底回收，避免活跃列表越堆越多。
        """
        if idle_seconds <= 0 or max_age_seconds <= 0:
            return []
        now = utcnow()
        stale: list[str] = []
        for request_id, session in list(self._active.items()):
            idle = (now - session.last_activity_at).total_seconds()
            age = (now - session.started_at).total_seconds()
            if idle >= idle_seconds and age >= max_age_seconds:
                stale.append(request_id)
        for request_id in stale:
            self.finish(
                request_id,
                status="abandoned",
                error_code=ErrorCode.CLIENT_DISCONNECTED,
                retention=retention,
            )
        if stale:
            log.warning("回收了 %d 个未正常收尾的会话：%s", len(stale), ", ".join(stale[:5]))
        return stale

    # ---------------------------------------------------------------- 快照
    def snapshot(self, *, stale_seconds: float = 300.0, recent_limit: int = 50) -> dict[str, Any]:
        active = [
            session.to_dict(stale_seconds=stale_seconds)
            for session in sorted(self._active.values(), key=lambda s: s.started_at, reverse=True)
        ]
        return {
            "type": "live",
            "rev": self._revision,
            "ts": to_iso(utcnow()),
            "active": active,
            "recent": list(self._recent)[:recent_limit],
            "stats": {
                "active": len(active),
                "peak_active": self._peak_active,
                "started": self._total_started,
                "finished": self._total_finished,
                "errors": self._total_errors,
                "tokens": self._total_tokens,
            },
        }

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ---------------------------------------------------------------- 订阅
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 落后太多的订阅者丢掉旧帧，下一帧是完整快照可自行恢复
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _touch(self) -> None:
        """标记状态有变化。

        `_revision` 每次都要自增（广播循环靠它判断「有没有变化」），但 tick 帧要
        节流：活跃会话每收到一个流式块都会走到这里，一次长回答就是上千次，全推给
        订阅者等于拿无用帧去挤占事件循环与浏览器。控制台的实时面板本来就走
        「按固定间隔推完整快照」，tick 只是轻量提示，攒到间隔再发不影响观感。
        """
        self._revision += 1
        if not self._subscribers:
            return
        now = time.monotonic()
        if now - self._last_tick_at < TICK_MIN_INTERVAL_SECONDS:
            return
        self._last_tick_at = now
        self.publish(
            {
                "type": "tick",
                "rev": self._revision,
                "active": len(self._active),
                "ts": to_iso(utcnow()),
            }
        )

    async def broadcast_loop(
        self,
        *,
        interval_ms: int = 1000,
        stale_seconds: float = 300.0,
        recent_limit: int = 50,
        sweep_idle_seconds: float = 600.0,
        sweep_max_age_seconds: float = 1800.0,
    ) -> None:
        """周期性推送完整快照；无变化时只在有订阅者时发心跳。

        每次 tick 顺带做一次僵尸会话回收，成本很低（活跃表通常只有个位数）。
        """
        last_rev = -1
        interval = max(0.2, interval_ms / 1000.0)
        while True:
            try:
                await asyncio.sleep(interval)
                self.sweep_stale(
                    idle_seconds=sweep_idle_seconds,
                    max_age_seconds=sweep_max_age_seconds,
                    retention=recent_limit,
                )
                if not self._subscribers:
                    last_rev = self._revision
                    continue
                if self._revision == last_rev and not self._active:
                    # 空闲期降低推送频率（每 5 个 tick 一次心跳）
                    if self._revision % 5 != 0:
                        continue
                last_rev = self._revision
                self.publish(self.snapshot(stale_seconds=stale_seconds, recent_limit=recent_limit))
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 广播循环必须存活
                log.exception("实时会话广播循环异常")
