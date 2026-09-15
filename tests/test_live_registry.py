"""实时会话注册表的独立测试：僵尸会话回收与快照口径。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from airelay.services.live import LiveSession
from airelay.timeutil import utcnow

pytestmark = pytest.mark.anyio


async def test_stale_session_is_reaped(ctx) -> None:
    session = ctx.live.start(LiveSession(request_id="req_stale", model="ghost", channel_name="c"))
    session.started_at = utcnow() - timedelta(seconds=1200)
    session.last_activity_at = utcnow() - timedelta(seconds=900)
    assert ctx.live.active_count == 1

    removed = ctx.live.sweep_stale(idle_seconds=600, max_age_seconds=900, retention=20)
    assert removed == ["req_stale"]
    assert ctx.live.active_count == 0
    recent = ctx.live.snapshot()["recent"]
    assert recent[0]["status"] == "abandoned"
    assert recent[0]["error_code"] == "CLIENT_DISCONNECTED"


async def test_fresh_session_is_not_reaped(ctx) -> None:
    ctx.live.start(LiveSession(request_id="req_live", model="busy"))
    assert ctx.live.sweep_stale(idle_seconds=600, max_age_seconds=900) == []
    assert ctx.live.active_count == 1


async def test_snapshot_reports_stalled_and_speed(ctx) -> None:
    session = ctx.live.start(LiveSession(request_id="req_1", model="fast", stream=True))
    session.started_at = utcnow() - timedelta(seconds=10)
    session.first_token_at = utcnow() - timedelta(seconds=4)
    session.last_activity_at = utcnow() - timedelta(seconds=400)
    session.prompt_tokens = 10
    session.completion_tokens = 40
    session.total_tokens = 50

    payload = ctx.live.snapshot(stale_seconds=300)["active"][0]
    assert payload["stalled"] is True
    assert payload["status"] == "stalled"
    assert payload["speed_tok_s"] > 0
    assert payload["first_token_ms"] > 0
    assert payload["idle_seconds"] >= 400
