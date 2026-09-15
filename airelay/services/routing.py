"""路由：候选渠道筛选、优先级/权重选择、故障熔断与粘滞。

对应方案 5 节的路由模块与 13 节的「重试与故障切换」：
同模型多渠道按 priority/weight 加权随机；失败时对可重试类错误换渠道重试，
鉴权/参数类错误直接透传不重试。
"""

from __future__ import annotations

import fnmatch
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import normalize_provider
from ..errors import ErrorCode, RelayError
from ..models import Channel
from ..settings import SettingsService
from .mapping import ResolvedModel

log = logging.getLogger(__name__)


def channel_serves(channel: Channel, model: str) -> bool:
    """渠道声明的模型白名单（glob）；空列表表示不限制。"""
    patterns = channel.model_patterns()
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in patterns)


def channel_serves_request(channel: Channel, resolved: ResolvedModel) -> bool:
    """白名单同时按「客户端请求名」和「上游真实模型」两种写法匹配。

    用户配别名是为了少打长 id，白名单里自然也会写别名（如 `fast`），
    而实际发往上游的是 `mock-gpt-small`——两边都要认，否则配了别名就用不了白名单。
    """
    if channel.model_patterns() == []:
        return True
    if channel_serves(channel, resolved.upstream):
        return True
    return resolved.requested != resolved.upstream and channel_serves(channel, resolved.requested)


@dataclass
class ChannelHealth:
    channel_id: str
    cooldown_until: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    failures: int = 0
    successes: int = 0
    last_latency_ms: float = 0.0

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "cooling": self.cooldown_remaining() > 0,
            "cooldown_remaining": round(self.cooldown_remaining(), 1),
            "last_error": self.last_error,
            "failures": self.failures,
            "successes": self.successes,
            "last_latency_ms": round(self.last_latency_ms, 1),
        }


@dataclass
class SelectionPlan:
    """一次请求的渠道尝试计划。"""

    resolved: ResolvedModel
    candidates: list[Channel] = field(default_factory=list)
    reason: str = ""

    @property
    def empty(self) -> bool:
        return not self.candidates


class Router:
    def __init__(self, *, settings: SettingsService) -> None:
        self.settings = settings
        self._health: dict[str, ChannelHealth] = {}
        self._sticky: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ 健康
    def health(self, channel_id: str) -> ChannelHealth:
        return self._health.setdefault(channel_id, ChannelHealth(channel_id=channel_id))

    def is_cooling(self, channel_id: str) -> bool:
        entry = self._health.get(channel_id)
        return bool(entry and entry.cooldown_remaining() > 0)

    def mark_success(self, channel_id: str, *, latency_ms: float = 0.0) -> None:
        entry = self.health(channel_id)
        entry.successes += 1
        entry.failures = 0
        entry.cooldown_until = 0.0
        if latency_ms:
            entry.last_latency_ms = latency_ms

    def mark_failure(self, channel_id: str, message: str, *, retryable: bool = True) -> None:
        entry = self.health(channel_id)
        entry.failures += 1
        entry.last_error = message[:300]
        entry.last_error_at = time.monotonic()
        cooldown = self.settings.get_int("gateway.channel_cooldown_seconds", 60)
        if retryable and cooldown > 0:
            # 连续失败时线性延长冷却，避免反复撞同一个坏渠道
            factor = min(max(entry.failures, 1), 5)
            entry.cooldown_until = time.monotonic() + cooldown * factor
        log.warning("渠道 %s 标记失败（第 %d 次）：%s", channel_id, entry.failures, message[:160])

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        return {cid: entry.to_dict() for cid, entry in self._health.items()}

    def clear_cooldown(self, channel_id: str | None = None) -> None:
        if channel_id is None:
            for entry in self._health.values():
                entry.cooldown_until = 0.0
                entry.failures = 0
            self._sticky.clear()
        else:
            entry = self.health(channel_id)
            entry.cooldown_until = 0.0
            entry.failures = 0

    # ------------------------------------------------------------------ 粘滞
    def _sticky_pick(self, key_id: str) -> str | None:
        minutes = self.settings.get_int("routing.sticky_minutes", 0)
        if minutes <= 0 or not key_id:
            return None
        entry = self._sticky.get(key_id)
        if not entry:
            return None
        channel_id, expires_at = entry
        if time.monotonic() >= expires_at:
            self._sticky.pop(key_id, None)
            return None
        return channel_id

    def _remember_sticky(self, key_id: str, channel_id: str) -> None:
        minutes = self.settings.get_int("routing.sticky_minutes", 0)
        if minutes <= 0 or not key_id:
            return
        self._sticky[key_id] = (channel_id, time.monotonic() + minutes * 60)

    # ------------------------------------------------------------------ 选择
    async def plan(
        self,
        session: AsyncSession,
        *,
        resolved: ResolvedModel,
        channels: Iterable[Channel],
        key_id: str = "",
        exclude: Iterable[str] = (),
    ) -> SelectionPlan:
        excluded = set(exclude)
        wanted_provider = normalize_provider(resolved.provider) if resolved.provider else ""
        bound_channel = resolved.channel_id

        candidates: list[Channel] = []
        skipped_cooling = 0
        skipped_model = 0
        skipped_provider = 0

        for channel in channels:
            if channel.channel_id in excluded:
                continue
            if channel.status != "active":
                continue
            if bound_channel and channel.channel_id != bound_channel:
                continue
            provider = normalize_provider(channel.provider_type)
            if wanted_provider and provider != wanted_provider:
                skipped_provider += 1
                continue
            if not channel_serves_request(channel, resolved):
                skipped_model += 1
                continue
            if self.is_cooling(channel.channel_id):
                skipped_cooling += 1
                continue
            candidates.append(channel)

        if not candidates:
            reason = "没有匹配的可用渠道"
            if skipped_cooling:
                reason = f"匹配的 {skipped_cooling} 个渠道正在熔断冷却中"
            elif skipped_model:
                reason = "没有渠道声明支持该模型（可在渠道的「模型白名单」里放开）"
            elif skipped_provider:
                reason = "没有渠道提供该协议类型"
            return SelectionPlan(resolved, [], reason)

        ordered = self._order(candidates)
        sticky_id = self._sticky_pick(key_id)
        if sticky_id:
            for position, channel in enumerate(ordered):
                if channel.channel_id == sticky_id:
                    ordered.insert(0, ordered.pop(position))
                    break

        limit = max(1, self.settings.get_int("routing.max_attempts_channels", 4))
        return SelectionPlan(resolved, ordered[:limit], "")

    def _order(self, candidates: list[Channel]) -> list[Channel]:
        """按 priority 升序分组，组内按 weight 加权随机。"""
        grouped: dict[int, list[Channel]] = {}
        for channel in candidates:
            grouped.setdefault(int(channel.priority or 0), []).append(channel)

        ordered: list[Channel] = []
        for priority in sorted(grouped.keys()):
            group = grouped[priority]
            ordered.extend(_weighted_shuffle(group))
        return ordered

    def remember_used(self, key_id: str, channel_id: str) -> None:
        self._remember_sticky(key_id, channel_id)

    # ------------------------------------------------------------------ 决策
    @staticmethod
    def should_retry(error: RelayError) -> bool:
        """鉴权 / 参数类错误不重试，避免放大上游限流（方案 13 节）。"""
        if not error.retryable:
            return False
        return error.code in {
            ErrorCode.UPSTREAM_ERROR,
            ErrorCode.UPSTREAM_TIMEOUT,
            ErrorCode.NO_CHANNEL_AVAILABLE,
            ErrorCode.RATE_LIMITED,
        }


def _weighted_shuffle(channels: list[Channel]) -> list[Channel]:
    """按权重做无放回加权抽样，得到完整顺序。"""
    pool = list(channels)
    ordered: list[Channel] = []
    while pool:
        weights = [max(0, int(channel.weight or 0)) for channel in pool]
        total = sum(weights)
        if total <= 0:
            random.shuffle(pool)
            ordered.extend(pool)
            break
        pick = random.uniform(0, total)
        cursor = 0.0
        chosen = len(pool) - 1
        for index, weight in enumerate(weights):
            cursor += weight
            if pick <= cursor:
                chosen = index
                break
        ordered.append(pool.pop(chosen))
    return ordered
