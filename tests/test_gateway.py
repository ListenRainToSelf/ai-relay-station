"""协议面端到端测试：鉴权、路由、三协议、流式、故障切换、计量。"""

from __future__ import annotations

import json

import pytest

from conftest import auth_header, create_channel, create_key

pytestmark = pytest.mark.anyio

CHAT = "/v1/chat/completions"


def simple_body(model: str = "fast", **extra) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": "你好"}]}
    body.update(extra)
    return body


# --------------------------------------------------------------------------- #
# 鉴权与基础错误
# --------------------------------------------------------------------------- #
async def test_missing_key_returns_401(client) -> None:
    response = await client.post(CHAT, json=simple_body())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_API_KEY"


async def test_invalid_key_returns_401(client, ctx) -> None:
    await create_key(ctx, name="k1")
    response = await client.post(CHAT, json=simple_body(), headers=auth_header("sk-relay-deadbeef-nope"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_API_KEY"


async def test_disabled_key_rejected(client, ctx) -> None:
    _, plaintext = await create_key(ctx, name="k-disabled", status="disabled")
    response = await client.post(CHAT, json=simple_body(), headers=auth_header(plaintext))
    assert response.status_code == 401


async def test_expired_key_returns_403(client, ctx) -> None:
    _, plaintext = await create_key(ctx, name="k-expired")
    async with ctx.session_factory() as session:
        from airelay.timeutil import utcnow
        from datetime import timedelta

        record = await ctx.keys.get_by_prefix(session, plaintext.split("-")[2])
        record.expires_at = utcnow() - timedelta(days=1)
        await session.commit()
    response = await client.post(CHAT, json=simple_body(), headers=auth_header(plaintext))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "KEY_EXPIRED"


async def test_no_channel_returns_503(client, ctx) -> None:
    _, plaintext = await create_key(ctx, name="k-no-channel")
    response = await client.post(CHAT, json=simple_body(), headers=auth_header(plaintext))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NO_CHANNEL_AVAILABLE"


async def test_missing_model_or_messages_returns_400(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k")
    headers = auth_header(plaintext)
    assert (await client.post(CHAT, json={"messages": []}, headers=headers)).status_code == 400
    assert (
        await client.post(CHAT, json={"model": "fast"}, headers=headers)
    ).status_code == 400


async def test_model_not_allowed_returns_403(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-limited", model_allowed=["allowed-*"])
    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MODEL_NOT_ALLOWED"


# --------------------------------------------------------------------------- #
# OpenAI 透传
# --------------------------------------------------------------------------- #
async def test_non_stream_passthrough_records_usage(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="openai-1", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-main")

    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 200
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == mock_state.REPLY_TEXT
    assert payload["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert payload["model"] == "fast"
    assert response.headers["x-airelay-channel"] == "openai-1"
    assert response.headers["x-request-id"].startswith("req_")

    # 上游确实收到了我们转发的请求
    calls = mock_state.calls_for("/v1/chat/completions")
    assert calls and calls[-1]["body"]["model"] == "fast"
    assert calls[-1]["body"]["messages"][0]["content"] == "你好"

    # 用量落到明细与统计
    await ctx.usage.drain()
    logs = await ctx.usage.recent(limit=5)
    assert logs[0]["status"] == "ok"
    assert logs[0]["total_tokens"] == 18
    assert logs[0]["stream"] is False
    assert logs[0]["model"] == "fast"

    stats = await ctx.usage.overview(hours=1)
    assert stats["requests"] == 1
    assert stats["total_tokens"] == 18
    by_model = await ctx.usage.by_model(hours=1)
    assert by_model[0]["model"] == "fast"
    assert by_model[0]["tokens"] == 18


async def test_stream_passthrough_emits_sse_with_usage(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="openai-stream", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-stream")

    async with client.stream(
        "POST", CHAT, json=simple_body("fast", stream=True), headers=auth_header(plaintext)
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in response.aiter_text()])

    frames = [line for line in raw.split("\n\n") if line.strip()]
    payloads = [
        json.loads(frame[6:]) for frame in frames if frame.startswith("data: ") and frame[6:].strip() != "[DONE]"
    ]
    text = "".join(
        (choice.get("delta") or {}).get("content") or ""
        for payload in payloads
        for choice in payload.get("choices", [])
    )
    assert text == mock_state.REPLY_TEXT
    assert raw.strip().endswith("data: [DONE]")
    # 上游没回 usage，网关补一个 usage chunk（默认开启 stream_include_usage）
    usage_chunks = [payload for payload in payloads if payload.get("usage")]
    assert usage_chunks, "应当补发 usage chunk"
    assert usage_chunks[-1]["usage"]["total_tokens"] > 0

    await ctx.usage.drain()
    logs = await ctx.usage.recent(limit=5)
    assert logs[0]["stream"] is True
    assert logs[0]["first_token_ms"] > 0
    assert logs[0]["total_tokens"] > 0
    # 会话已从活跃表移出，进入最近完成列表
    snapshot = ctx.live.snapshot()
    assert snapshot["stats"]["active"] == 0
    assert snapshot["recent"], "完成的会话应出现在最近请求里"


async def test_stream_forwards_upstream_usage_chunk_without_duplicating(
    client, ctx, mock_upstream, mock_state
) -> None:
    await create_channel(ctx, name="openai-usage", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-usage")
    body = simple_body("fast", stream=True, stream_options={"include_usage": True})
    async with client.stream("POST", CHAT, json=body, headers=auth_header(plaintext)) as response:
        raw = "".join([chunk async for chunk in response.aiter_text()])
    payloads = [
        json.loads(frame[6:])
        for frame in raw.split("\n\n")
        if frame.startswith("data: ") and frame[6:].strip() != "[DONE]"
    ]
    usage_chunks = [payload for payload in payloads if payload.get("usage")]
    assert len(usage_chunks) == 1, "上游已给 usage 时不应重复补发"
    assert usage_chunks[0]["usage"]["total_tokens"] == 18
    await ctx.usage.drain()
    logs = await ctx.usage.recent(limit=5)
    assert logs[0]["total_tokens"] == 18


async def test_max_tokens_reaches_upstream(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-tokens")
    await client.post(CHAT, json=simple_body("fast", max_tokens=64), headers=auth_header(plaintext))
    assert mock_state.calls_for("/v1/chat/completions")[-1]["body"]["max_tokens"] == 64


# --------------------------------------------------------------------------- #
# 三种协议
# --------------------------------------------------------------------------- #
async def test_anthropic_channel_translation(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(
        ctx, name="claude-1", provider_type="anthropic", base_url=mock_upstream, api_key="sk-ant"
    )
    _, plaintext = await create_key(ctx, name="k-claude")
    response = await client.post(
        CHAT,
        json={
            "model": "claude-4",
            "messages": [
                {"role": "system", "content": "简洁"},
                {"role": "user", "content": "你好"},
            ],
        },
        headers=auth_header(plaintext),
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == mock_state.REPLY_TEXT
    assert response.json()["usage"]["total_tokens"] == 18
    sent = mock_state.calls_for("/v1/messages")[-1]["body"]
    assert sent["system"] == "简洁"
    assert sent["max_tokens"] == 4096  # 走 gateway.default_max_tokens 兜底
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]


async def test_anthropic_channel_stream(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="claude-s", provider_type="anthropic", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-claude-s")
    async with client.stream(
        "POST", CHAT, json=simple_body("claude-4", stream=True), headers=auth_header(plaintext)
    ) as response:
        raw = "".join([chunk async for chunk in response.aiter_text()])
    payloads = [
        json.loads(frame[6:])
        for frame in raw.split("\n\n")
        if frame.startswith("data: ") and frame[6:].strip() != "[DONE]"
    ]
    text = "".join(
        (choice.get("delta") or {}).get("content") or ""
        for payload in payloads
        for choice in payload.get("choices", [])
    )
    assert text == mock_state.REPLY_TEXT
    finish = [
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finish[-1] == "stop"
    await ctx.usage.drain()
    assert (await ctx.usage.recent(limit=1))[0]["provider_type"] == "anthropic"


async def test_gemini_channel_translation_and_stream(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="gemini-1", provider_type="gemini", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-gemini")

    response = await client.post(CHAT, json=simple_body("gemini-2.5-flash"), headers=auth_header(plaintext))
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == mock_state.REPLY_TEXT
    sent = mock_state.calls_for("/v1beta/models")[-1]["body"]
    assert sent["generationConfig"]["maxOutputTokens"] == 4096
    assert sent["contents"][0]["parts"][0]["text"] == "你好"

    async with client.stream(
        "POST", CHAT, json=simple_body("gemini-2.5-flash", stream=True), headers=auth_header(plaintext)
    ) as stream_response:
        raw = "".join([chunk async for chunk in stream_response.aiter_text()])
    payloads = [
        json.loads(frame[6:])
        for frame in raw.split("\n\n")
        if frame.startswith("data: ") and frame[6:].strip() != "[DONE]"
    ]
    text = "".join(
        (choice.get("delta") or {}).get("content") or ""
        for payload in payloads
        for choice in payload.get("choices", [])
    )
    assert text == mock_state.REPLY_TEXT


# --------------------------------------------------------------------------- #
# 路由：模型别名、优先级、权重、故障切换
# --------------------------------------------------------------------------- #
async def test_model_alias_is_resolved_and_hidden(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    async with ctx.session_factory() as session:
        await ctx.mapping.create(
            session, {"alias": "fast", "upstream_model": "mock-gpt-small", "note": "测试别名"}
        )
    _, plaintext = await create_key(ctx, name="k-alias")
    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 200
    # 上游收到的是真实模型 id，客户端看到的是别名
    assert mock_state.calls_for("/v1/chat/completions")[-1]["body"]["model"] == "mock-gpt-small"
    assert response.json()["model"] == "fast"
    assert response.headers["x-airelay-upstream-model"] == "mock-gpt-small"


async def test_failover_switches_channel_on_retryable_error(
    client, ctx, mock_upstream, mock_state
) -> None:
    await create_channel(ctx, name="bad", provider_type="openai", base_url=mock_upstream, priority=0, weight=1)
    await create_channel(ctx, name="good", provider_type="openai", base_url=mock_upstream, priority=1, weight=1)
    _, plaintext = await create_key(ctx, name="k-failover")
    mock_state.STATE["fail_times"] = 1

    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 200
    assert response.headers["x-airelay-attempts"] == "2"
    assert mock_state.STATE["fail_times"] == 0

    await ctx.usage.drain()
    log = (await ctx.usage.recent(limit=1))[0]
    assert log["attempts"] == 2
    assert log["status"] == "ok"


async def test_all_channels_failing_returns_502(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="bad-1", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-all-fail")
    mock_state.STATE["fail_times"] = 5
    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
    await ctx.usage.drain()
    log = (await ctx.usage.recent(limit=1))[0]
    assert log["status"] == "error"
    assert log["error_code"] == "UPSTREAM_ERROR"


async def test_channel_model_whitelist_filters_candidates(client, ctx, mock_upstream) -> None:
    await create_channel(
        ctx, name="only-large", provider_type="openai", base_url=mock_upstream, models=["mock-gpt-large"]
    )
    _, plaintext = await create_key(ctx, name="k-whitelist")
    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 503
    ok = await client.post(CHAT, json=simple_body("mock-gpt-large"), headers=auth_header(plaintext))
    assert ok.status_code == 200


async def test_whitelist_accepts_alias_name(client, ctx, mock_upstream, mock_state) -> None:
    """白名单按别名书写时也要能命中（别名的意义就是让用户只记短名）。"""
    await create_channel(
        ctx, name="alias-whitelist", provider_type="openai", base_url=mock_upstream, models=["fast"]
    )
    async with ctx.session_factory() as session:
        await ctx.mapping.create(session, {"alias": "fast", "upstream_model": "mock-gpt-small"})
    _, plaintext = await create_key(ctx, name="k-alias-whitelist")
    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 200
    assert mock_state.calls_for("/v1/chat/completions")[-1]["body"]["model"] == "mock-gpt-small"


async def test_priority_wins_over_weight(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="primary", provider_type="openai", base_url=mock_upstream, priority=0)
    await create_channel(ctx, name="backup", provider_type="openai", base_url=mock_upstream, priority=9, weight=99)
    _, plaintext = await create_key(ctx, name="k-priority")
    for _ in range(3):
        response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
        assert response.headers["x-airelay-channel"] == "primary"


async def test_cooldown_channel_is_skipped(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="flaky", provider_type="openai", base_url=mock_upstream)
    await create_channel(ctx, name="stable", provider_type="openai", base_url=mock_upstream, priority=1)
    _, plaintext = await create_key(ctx, name="k-cooldown")
    mock_state.STATE["fail_times"] = 1

    first = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert first.headers["x-airelay-channel"] == "stable"
    # flaky 进入冷却，后续请求不会再撞它
    assert ctx.router.is_cooling(
        next(
            channel_id
            for channel_id, entry in ctx.router.health_snapshot().items()
            if entry["failures"] > 0
        )
    )
    second = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert second.headers["x-airelay-channel"] == "stable"


# --------------------------------------------------------------------------- #
# 额度与限流
# --------------------------------------------------------------------------- #
async def test_quota_exceeded_after_usage(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    async with ctx.session_factory() as session:
        await ctx.settings.update(
            session,
            {"pricing.models": {"fast": {"prompt": 1.0, "completion": 1.0}}},
        )
    # 每次请求成本 = (11 + 7) / 1e6 美元 = 18 µ$，配额只给 10 µ$
    _, plaintext = await create_key(ctx, name="k-quota", quota_limit=10)

    first = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert first.status_code == 200
    await ctx.usage.drain()

    second = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "QUOTA_EXCEEDED"

    async with ctx.session_factory() as session:
        record = await ctx.keys.get_by_prefix(session, plaintext.split("-")[2])
        assert record.quota_used == 18


async def test_rpm_limit_returns_429_with_retry_after(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-rpm", rpm_limit=1)
    first = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert first.status_code == 200
    second = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMITED"
    assert int(second.headers["retry-after"]) >= 1


async def test_global_rpm_limit(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    async with ctx.session_factory() as session:
        await ctx.settings.update(session, {"ratelimit.global_rpm": 1})
    _, key_a = await create_key(ctx, name="a")
    _, key_b = await create_key(ctx, name="b")
    assert (await client.post(CHAT, json=simple_body("fast"), headers=auth_header(key_a))).status_code == 200
    blocked = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(key_b))
    assert blocked.status_code == 429
    assert blocked.json()["error"]["details"]["scope"] == "global"


# --------------------------------------------------------------------------- #
# 模型列表与旧版补全
# --------------------------------------------------------------------------- #
async def test_models_endpoint_lists_aliases(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    async with ctx.session_factory() as session:
        await ctx.mapping.create(session, {"alias": "fast", "upstream_model": "mock-gpt-small"})
    _, plaintext = await create_key(ctx, name="k-models")
    response = await client.get("/v1/models", headers=auth_header(plaintext))
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "fast" in ids
    detail = await client.get("/v1/models/fast", headers=auth_header(plaintext))
    assert detail.json()["upstream_model"] == "mock-gpt-small"


async def test_legacy_completions_maps_prompt_to_chat(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k-legacy")
    response = await client.post(
        "/v1/completions",
        json={"model": "fast", "prompt": "写一句话", "max_tokens": 32},
        headers=auth_header(plaintext),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "text_completion"
    assert payload["choices"][0]["text"] == mock_state.REPLY_TEXT
    assert mock_state.calls_for("/v1/chat/completions")[-1]["body"]["messages"] == [
        {"role": "user", "content": "写一句话"}
    ]


async def test_non_ascii_channel_name_does_not_break_headers(
    client, ctx, mock_upstream, mock_state
) -> None:
    """渠道名允许中文：HTTP 头只能放 latin-1，网关必须退回 id 而不是直接 500。"""
    await create_channel(
        ctx, name="深度求索 主力通道 ✅", provider_type="openai", base_url=mock_upstream
    )
    _, plaintext = await create_key(ctx, name="k-unicode")
    response = await client.post(CHAT, json=simple_body("fast"), headers=auth_header(plaintext))
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == mock_state.REPLY_TEXT
    assert response.headers["x-airelay-channel"].isascii()
    assert response.headers["x-airelay-channel-id"].startswith("ch_")

    async with client.stream(
        "POST", CHAT, json=simple_body("fast", stream=True), headers=auth_header(plaintext)
    ) as stream_response:
        assert stream_response.status_code == 200
        raw = "".join([chunk async for chunk in stream_response.aiter_text()])
    assert "data: [DONE]" in raw
    streamed = "".join(
        (choice.get("delta") or {}).get("content", "")
        for frame in raw.split("\n\n")
        if frame.startswith("data: ") and frame[6:].strip() != "[DONE]"
        for choice in (json.loads(frame[6:]).get("choices") or [])
    )
    assert streamed == mock_state.REPLY_TEXT
    # 会话必须正常收尾，不能留在活跃表里
    assert ctx.live.active_count == 0
