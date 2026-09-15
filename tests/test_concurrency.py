"""并发与筛选相关测试。

并发部分针对一个真实缺陷：同一个 Key 的多个请求在记账时是「先读后写」，
并发下会互相覆盖，导致已用额度与请求数偏少。现在改成 SQL 层自增，
这里就把「并发之后账目必须精确」钉住。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from conftest import auth_header, create_channel, create_key
from airelay.models import ApiKey, ApiKeyStat, UsageLog

pytestmark = pytest.mark.anyio

ADMIN = "/api/admin"
CHAT = "/v1/chat/completions"


def body(model: str = "fast", **extra) -> dict:
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# 并发：同一个本地 Key
# --------------------------------------------------------------------------- #
async def test_same_key_concurrent_calls_all_succeed(client, ctx, mock_upstream, mock_state) -> None:
    """同一个 Key 并发 20 个请求：全部成功，且上游确实被并发打到。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="并发", quota_limit=0)
    headers = auth_header(plaintext)

    mock_state.STATE["delay"] = 0.15  # 让请求真的重叠，而不是排队串行

    async def one(index: int):
        return await client.post(CHAT, json=body("fast"), headers=headers)

    responses = await asyncio.gather(*[one(i) for i in range(20)])
    codes = [response.status_code for response in responses]
    assert codes == [200] * 20, f"并发调用出现失败：{codes}"
    assert all(response.json()["choices"][0]["message"]["content"] for response in responses)

    calls = mock_state.calls_for("/v1/chat/completions")
    assert len(calls) == 20, f"上游应当收到 20 次调用，实际 {len(calls)}"


async def test_concurrent_accounting_is_exact(client, ctx, mock_upstream) -> None:
    """并发之后的账目必须精确：请求数与已用额度都不能少记。

    这是回归点：原来的「先读后写」在 20 并发下会丢更新（只记到个位数）。
    """
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    async with ctx.session_factory() as session:
        # 每次请求 11 输入 + 7 输出 = 18 µ$（单价 1 美元/百万 token）
        await ctx.settings.update(
            session, {"pricing.models": {"fast": {"prompt": 1.0, "completion": 1.0}}}
        )
    public, plaintext = await create_key(ctx, name="并发记账", quota_limit=0)
    headers = auth_header(plaintext)

    rounds = 20
    responses = await asyncio.gather(*[client.post(CHAT, json=body("fast"), headers=headers) for _ in range(rounds)])
    assert all(response.status_code == 200 for response in responses)

    await ctx.usage.drain()
    async with ctx.session_factory() as session:
        record = (
            await session.execute(select(ApiKey).where(ApiKey.key_id == public["key_id"]))
        ).scalar_one()
        assert record.total_requests == rounds, f"请求数少记：{record.total_requests} != {rounds}"
        assert record.quota_used == rounds * 18, f"额度少记：{record.quota_used} != {rounds * 18}"
        assert record.last_used_at is not None

        logs = (await session.execute(select(UsageLog).where(UsageLog.key_id == public["key_id"]))).scalars().all()
        assert len(logs) == rounds, f"明细行数不足：{len(logs)}"
        assert all(row.status == "ok" and row.total_tokens == 18 for row in logs)

        stats = (
            await session.execute(select(ApiKeyStat).where(ApiKeyStat.key_id == public["key_id"]))
        ).scalars().all()
        assert sum(row.requests for row in stats) == rounds
        assert sum(row.cost_units for row in stats) == rounds * 18


async def test_concurrent_streams_same_key(client, ctx, mock_upstream, mock_state) -> None:
    """流式并发同样要能同时跑，且各自把用量记全。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="并发流式")
    headers = auth_header(plaintext)
    mock_state.STATE["stream_delay"] = 0.05

    async def stream_once() -> str:
        collected = ""
        async with client.stream("POST", CHAT, json=body("fast", stream=True), headers=headers) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                for choice in chunk.get("choices") or []:
                    collected += (choice.get("delta") or {}).get("content") or ""
        return collected

    results = await asyncio.gather(*[stream_once() for _ in range(6)])
    assert all(text == mock_state.REPLY_TEXT for text in results), results

    await ctx.usage.drain()
    logs = await ctx.usage.recent(limit=20)
    assert len(logs) == 6
    assert all(row["stream"] and row["status"] == "ok" for row in logs)


async def test_concurrent_different_keys_are_isolated(client, ctx, mock_upstream) -> None:
    """不同 Key 并发时账目互不串号。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    first, first_key = await create_key(ctx, name="甲")
    second, second_key = await create_key(ctx, name="乙")
    payload = body("fast")

    tasks = []
    for _ in range(5):
        tasks.append(client.post(CHAT, json=payload, headers=auth_header(first_key)))
        tasks.append(client.post(CHAT, json=payload, headers=auth_header(second_key)))
    responses = await asyncio.gather(*tasks)
    assert all(response.status_code == 200 for response in responses)
    await ctx.usage.drain()

    async with ctx.session_factory() as session:
        for public, expected in ((first, 5), (second, 5)):
            record = (
                await session.execute(select(ApiKey).where(ApiKey.key_id == public["key_id"]))
            ).scalar_one()
            assert record.total_requests == expected, f"{public['name']} 记了 {record.total_requests}"


async def test_quota_still_applies_under_concurrency(client, ctx, mock_upstream) -> None:
    """并发不能绕过配额：额度用尽后后续请求必须被拒。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    async with ctx.session_factory() as session:
        await ctx.settings.update(
            session, {"pricing.models": {"fast": {"prompt": 1.0, "completion": 1.0}}}
        )
    # 每次 18 µ$，配额给 20 µ$。配额是「请求前检查 + 请求后记账」，
    # 所以同一瞬间在飞的并发请求会略微超发（这里 4 个都放行），
    # 但账目必须如实记全，且之后的请求一定被挡住——这是刻意的语义。

    _, plaintext = await create_key(ctx, name="并发配额", quota_limit=20)
    headers = auth_header(plaintext)

    responses = await asyncio.gather(*[client.post(CHAT, json=body("fast"), headers=headers) for _ in range(4)])
    assert all(response.status_code == 200 for response in responses)
    await ctx.usage.drain()

    blocked = await client.post(CHAT, json=body("fast"), headers=headers)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "QUOTA_EXCEEDED"

    async with ctx.session_factory() as session:
        record = (
            await session.execute(select(ApiKey).where(ApiKey.name == "并发配额"))
        ).scalar_one()
        assert record.quota_used == 4 * 18


# --------------------------------------------------------------------------- #
# 统计筛选：按本地密钥 / 按模型
# --------------------------------------------------------------------------- #
async def test_stats_filter_by_key_and_model(client, admin_headers, ctx, mock_upstream) -> None:
    """筛选条件要真的作用到总览、序列、聚合表上。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    async with ctx.session_factory() as session:
        await ctx.mapping.create(session, {"alias": "fast", "upstream_model": "mock-gpt-small"})
        await ctx.mapping.create(session, {"alias": "smart", "upstream_model": "mock-gpt-large"})

    key_a, plain_a = await create_key(ctx, name="甲")
    key_b, plain_b = await create_key(ctx, name="乙")

    # 甲：2 次 fast；乙：1 次 smart
    for _ in range(2):
        await client.post(CHAT, json=body("fast"), headers=auth_header(plain_a))
    await client.post(CHAT, json=body("smart"), headers=auth_header(plain_b))
    await ctx.usage.drain()

    # --- 不筛选：看到两个模型、两个密钥 ---
    everything = (await client.get(f"{ADMIN}/stats?hours=1", headers=admin_headers)).json()
    assert everything["overview"]["requests"] == 3
    assert {row["model"] for row in everything["by_model"]} == {"fast", "smart"}
    assert len(everything["by_key"]) == 2
    assert set(everything["options"]["models"]) == {"fast", "smart"}
    assert {item["name"] for item in everything["options"]["keys"]} == {"甲", "乙"}

    # --- 按密钥筛选 ---
    only_a = (await client.get(
        f"{ADMIN}/stats?hours=1&key_id={key_a['key_id']}", headers=admin_headers
    )).json()
    assert only_a["overview"]["requests"] == 2
    assert [row["model"] for row in only_a["by_model"]] == ["fast"]
    assert len(only_a["by_key"]) == 1 and only_a["by_key"][0]["name"] == "甲"

    # --- 按模型筛选 ---
    only_smart = (await client.get(f"{ADMIN}/stats?hours=1&model=smart", headers=admin_headers)).json()
    assert only_smart["overview"]["requests"] == 1
    assert [row["model"] for row in only_smart["by_model"]] == ["smart"]
    assert only_smart["by_key"][0]["name"] == "乙", "按模型筛选后应当只剩用过它的密钥"

    # --- 密钥 + 模型同时筛 ---
    both = (await client.get(
        f"{ADMIN}/stats?hours=1&key_id={key_b['key_id']}&model=fast", headers=admin_headers
    )).json()
    assert both["overview"]["requests"] == 0
    assert both["series"] == []

    # --- 按模型对比：返回多条序列，bucket 对齐 ---
    grouped = (await client.get(
        f"{ADMIN}/stats?hours=1&bucket=hour&group_by=model", headers=admin_headers
    )).json()
    assert "series_by_model" in grouped
    group = grouped["series_by_model"]
    assert {item["model"] for item in group["series"]} == {"fast", "smart"}
    assert len(group["buckets"]) >= 1
    for item in group["series"]:
        assert len(item["points"]) == len(group["buckets"])
        assert item["color"]
    fast_series = next(item for item in group["series"] if item["model"] == "fast")
    assert sum(point["requests"] for point in fast_series["points"]) == 2

    # --- 筛选条件回显 ---
    assert grouped["filters"]["group_by"] == "model"
    assert grouped["filters"]["hours"] == 1.0


async def test_stats_unknown_filter_returns_empty(client, admin_headers, ctx, mock_upstream) -> None:
    """筛选到不存在的密钥/模型时给空结果而不是报错。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    empty = (await client.get(
        f"{ADMIN}/stats?hours=1&key_id=key_not_exists&model=nope", headers=admin_headers
    )).json()
    assert empty["overview"]["requests"] == 0
    assert empty["series"] == []
    assert empty["by_model"] == []
    assert empty["series_by_model"]["series"] == [] if "series_by_model" in empty else True
