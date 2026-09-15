"""管理面测试：会话、密钥、渠道、别名、设置、统计、余额、运维接口。"""

from __future__ import annotations

import pytest

from conftest import auth_header, create_channel, create_key

pytestmark = pytest.mark.anyio

ADMIN = "/api/admin"


# --------------------------------------------------------------------------- #
# 会话与访问控制
# --------------------------------------------------------------------------- #
async def test_loopback_session_is_issued_automatically(client) -> None:
    response = await client.get(f"{ADMIN}/session")
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["auto"] is True
    assert "airelay_session" in response.cookies


async def test_remote_requires_token(remote_client, ctx) -> None:
    bootstrap = await remote_client.get(f"{ADMIN}/session")
    assert bootstrap.json() == {
        "authenticated": False,
        "requires_token": True,
        "client": "203.0.113.7",
    }
    denied = await remote_client.get(f"{ADMIN}/system")
    assert denied.status_code == 401
    assert denied.json()["code"] == "UNAUTHORIZED"

    wrong = await remote_client.post(f"{ADMIN}/session", json={"token": "wrong-token"})
    assert wrong.status_code == 401

    ok = await remote_client.post(f"{ADMIN}/session", json={"token": ctx.admin_token})
    assert ok.status_code == 200 and ok.json()["authenticated"] is True
    # 登录后拿到 Cookie，后续请求即可通过
    authorized = await remote_client.get(f"{ADMIN}/system")
    assert authorized.status_code == 200
    assert authorized.json()["version"]


async def test_header_token_works_for_scripts(remote_client, ctx) -> None:
    response = await remote_client.get(f"{ADMIN}/system", headers={"X-Admin-Token": ctx.admin_token})
    assert response.status_code == 200


async def test_logout_clears_cookie(client) -> None:
    await client.get(f"{ADMIN}/session")
    await client.delete(f"{ADMIN}/session")
    assert not client.cookies.get("airelay_session")


async def test_system_info_shape(client, admin_headers, mock_upstream) -> None:
    response = await client.get(f"{ADMIN}/system", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["app"] and payload["version"]
    assert payload["openai_base_url"].endswith("/v1")
    assert {item["type"] for item in payload["providers"]} >= {"openai", "anthropic", "gemini", "deepseek"}
    assert set(payload["counts"]) == {"channels", "keys", "model_maps", "active_sessions", "ws_subscribers"}


# --------------------------------------------------------------------------- #
# 密钥
# --------------------------------------------------------------------------- #
async def test_key_lifecycle(client, admin_headers) -> None:
    created = await client.post(
        f"{ADMIN}/keys",
        json={
            "name": "给 Cursor 用",
            "quota_limit": 5_000_000,
            "model_allowed": ["fast", "claude-*"],
            "rpm_limit": 30,
            "expires_in_days": 30,
            "note": "本机编辑器",
        },
        headers=admin_headers,
    )
    assert created.status_code == 200
    payload = created.json()
    plaintext = payload["plaintext"]
    key_id = payload["key"]["key_id"]
    assert plaintext.startswith("sk-relay-")
    assert payload["key"]["masked"].endswith("****************")
    assert payload["key"]["model_allowed"] == ["fast", "claude-*"]
    assert payload["key"]["expires_at"]

    listing = await client.get(f"{ADMIN}/keys", headers=admin_headers)
    items = listing.json()["items"]
    assert listing.json()["total"] == 1
    assert items[0]["key_id"] == key_id
    assert "plaintext" not in items[0]
    assert items[0]["quota"]["limit"] == 5_000_000
    assert "ratelimit" in items[0]

    detail = await client.get(f"{ADMIN}/keys/{key_id}", headers=admin_headers)
    assert detail.json()["usage"]["by_model"] == []
    assert detail.json()["totals"]["requests"] == 0

    updated = await client.put(
        f"{ADMIN}/keys/{key_id}", json={"status": "disabled", "quota_limit": 100}, headers=admin_headers
    )
    assert updated.json()["status"] == "disabled"
    assert updated.json()["quota_limit"] == 100

    deleted = await client.delete(f"{ADMIN}/keys/{key_id}", headers=admin_headers)
    assert deleted.json()["ok"] is True
    assert (await client.get(f"{ADMIN}/keys", headers=admin_headers)).json()["total"] == 0


async def test_key_quota_reset_and_detail_usage(client, admin_headers, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    public, plaintext = await create_key(ctx, name="k", quota_limit=1_000_000)
    await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(plaintext),
    )
    await ctx.usage.drain()

    detail = await client.get(f"{ADMIN}/keys/{public['key_id']}", headers=admin_headers)
    body = detail.json()
    assert body["total_requests"] == 1
    assert body["usage"]["by_model"][0]["model"] == "fast"
    assert body["totals"]["requests"] == 1

    reset = await client.post(f"{ADMIN}/keys/{public['key_id']}/reset-usage", headers=admin_headers)
    assert reset.json()["key"]["quota_used"] == 0


async def test_key_create_requires_name(client, admin_headers) -> None:
    response = await client.post(f"{ADMIN}/keys", json={"name": ""}, headers=admin_headers)
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 渠道
# --------------------------------------------------------------------------- #
async def test_channel_lifecycle_and_key_masking(client, admin_headers, mock_upstream) -> None:
    created = await client.post(
        f"{ADMIN}/channels",
        json={
            "name": "DeepSeek 主力",
            "provider_type": "deepseek",
            "base_url": mock_upstream,
            "api_key": "sk-upstream-secret-123456",
            "priority": 0,
            "weight": 3,
            "models": ["fast", "deepseek-*"],
            "note": "日常主力渠道",
        },
        headers=admin_headers,
    )
    assert created.status_code == 200
    channel = created.json()
    channel_id = channel["channel_id"]
    assert channel["models"] == ["fast", "deepseek-*"]
    assert "sk-upstream-secret" not in str(channel)
    assert channel["has_api_key"] is True

    listing = await client.get(f"{ADMIN}/channels", headers=admin_headers)
    item = listing.json()["items"][0]
    assert item["api_key_hint"].startswith("sk-ups")
    assert item["api_key_hint"].endswith("3456")
    assert "health" in item

    updated = await client.put(
        f"{ADMIN}/channels/{channel_id}",
        json={"weight": 5, "extra_headers": {"x-custom": "1"}},
        headers=admin_headers,
    )
    assert updated.json()["weight"] == 5
    assert updated.json()["extra_headers"] == {"x-custom": "1"}
    # 不传 api_key 时原密钥保持不变
    assert updated.json()["has_api_key"] is True
    assert updated.json()["api_key_hint"].endswith("3456")

    assert (await client.delete(f"{ADMIN}/channels/{channel_id}", headers=admin_headers)).json()["ok"]


async def test_channel_requires_api_key_and_base_url(client, admin_headers) -> None:
    missing_key = await client.post(
        f"{ADMIN}/channels",
        json={"name": "x", "provider_type": "openai", "base_url": "http://x"},
        headers=admin_headers,
    )
    assert missing_key.status_code == 400
    missing_base = await client.post(
        f"{ADMIN}/channels",
        json={"name": "x", "provider_type": "openai-compatible", "base_url": "", "api_key": "k"},
        headers=admin_headers,
    )
    assert missing_base.status_code == 400


async def test_channel_probe_and_model_listing(client, admin_headers, mock_upstream) -> None:
    created = await client.post(
        f"{ADMIN}/channels",
        json={
            "name": "probe-me",
            "provider_type": "openai",
            "base_url": mock_upstream,
            "api_key": "sk-x",
            "models": ["mock-gpt-small"],
        },
        headers=admin_headers,
    )
    channel_id = created.json()["channel_id"]

    probe = await client.post(
        f"{ADMIN}/channels/{channel_id}/probe", json={"model": ""}, headers=admin_headers
    )
    assert probe.status_code == 200
    assert probe.json()["ok"] is True
    assert probe.json()["latency_ms"] >= 0
    assert probe.json()["usage"]["total_tokens"] == 18

    models = await client.get(f"{ADMIN}/channels/{channel_id}/models", headers=admin_headers)
    assert [item["id"] for item in models.json()["items"]] == ["mock-gpt-large", "mock-gpt-small"]


async def test_probe_reports_upstream_failure(client, admin_headers, mock_upstream, mock_state) -> None:
    created = await client.post(
        f"{ADMIN}/channels",
        json={"name": "bad", "provider_type": "openai", "base_url": mock_upstream, "api_key": "sk-x"},
        headers=admin_headers,
    )
    channel_id = created.json()["channel_id"]
    mock_state.STATE["fail_times"] = 1
    mock_state.STATE["fail_status"] = 401
    probe = await client.post(
        f"{ADMIN}/channels/{channel_id}/probe", json={"model": "mock-gpt-small"}, headers=admin_headers
    )
    assert probe.json()["ok"] is False
    assert probe.json()["http_status"] == 401


async def test_reset_channel_cooldown(client, admin_headers, ctx, mock_upstream, mock_state) -> None:
    created = await create_channel(ctx, name="flaky", provider_type="openai", base_url=mock_upstream)
    mock_state.STATE["fail_times"] = 1
    _, plaintext = await create_key(ctx, name="k")
    await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(plaintext),
    )
    assert ctx.router.is_cooling(created["channel_id"])
    response = await client.post(
        f"{ADMIN}/channels/{created['channel_id']}/reset-cooldown", headers=admin_headers
    )
    assert response.json()["health"]["cooling"] is False
    assert not ctx.router.is_cooling(created["channel_id"])


# --------------------------------------------------------------------------- #
# 模型别名
# --------------------------------------------------------------------------- #
async def test_model_map_crud_and_resolve(client, admin_headers, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)

    created = await client.post(
        f"{ADMIN}/models/map",
        json={"alias": "claude-4", "upstream_model": "claude-sonnet-4-5-20250929", "provider_type": "openai"},
        headers=admin_headers,
    )
    assert created.status_code == 200
    map_id = created.json()["id"]
    assert created.json()["wildcard"] is False

    duplicate = await client.post(
        f"{ADMIN}/models/map", json={"alias": "claude-4", "upstream_model": "x"}, headers=admin_headers
    )
    assert duplicate.status_code == 400

    wildcard = await client.post(
        f"{ADMIN}/models/map", json={"alias": "gpt-*", "upstream_model": "mock-gpt-large"}, headers=admin_headers
    )
    assert wildcard.json()["wildcard"] is True

    listing = await client.get(f"{ADMIN}/models/map", headers=admin_headers)
    assert {item["alias"] for item in listing.json()["items"]} == {"claude-4", "gpt-*"}

    resolved = await client.get(f"{ADMIN}/models/resolve?model=gpt-4o", headers=admin_headers)
    body = resolved.json()
    assert body["resolved"]["upstream"] == "mock-gpt-large"
    assert body["resolved"]["alias"] == "gpt-*"
    assert body["candidates"][0]["name"] == "c"
    assert body["reason"] == ""

    hit = await client.get(f"{ADMIN}/models/resolve?model=claude-4", headers=admin_headers)
    assert hit.json()["resolved"]["upstream"] == "claude-sonnet-4-5-20250929"

    missing = await client.get(f"{ADMIN}/models/resolve?model=nope", headers=admin_headers)
    assert missing.json()["resolved"]["mapped"] is False

    updated = await client.put(
        f"{ADMIN}/models/map/{map_id}", json={"upstream_model": "new-id"}, headers=admin_headers
    )
    assert updated.json()["upstream_model"] == "new-id"
    assert (await client.delete(f"{ADMIN}/models/map/{map_id}", headers=admin_headers)).json()["ok"]


async def test_model_map_import(client, admin_headers) -> None:
    response = await client.post(
        f"{ADMIN}/models/map/import",
        json={
            "entries": [
                {"alias": "fast", "upstream_model": "deepseek-v4-flash"},
                {"alias": "smart", "upstream_model": "deepseek-reasoner"},
            ]
        },
        headers=admin_headers,
    )
    assert response.json() == {"ok": True, "created": 2, "updated": 0, "total": 2}
    again = await client.post(
        f"{ADMIN}/models/map/import",
        json={"entries": [{"alias": "fast", "upstream_model": "changed"}]},
        headers=admin_headers,
    )
    assert again.json()["updated"] == 1


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
async def test_settings_get_schema_and_update(client, admin_headers, ctx) -> None:
    got = await client.get(f"{ADMIN}/settings", headers=admin_headers)
    body = got.json()
    assert body["values"]["network.port"] == 8000
    groups = {group["group"] for group in body["schema"]}
    assert {"network", "gateway", "ratelimit", "monitoring", "balance", "pricing", "logs", "ui"} <= groups
    assert body["restart_managed"] is False

    updated = await client.put(
        f"{ADMIN}/settings",
        json={"values": {"network.port": 9123, "logs.level": "DEBUG", "pricing.models": {"fast": {"prompt": 2, "completion": 4}}}},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    changed = {item["key"]: item for item in updated.json()["changed"]}
    assert changed["network.port"]["requires_restart"] is True
    assert changed["network.port"]["new"] == 9123
    assert updated.json()["pending_restart"] is True

    after = (await client.get(f"{ADMIN}/settings", headers=admin_headers)).json()
    assert after["values"]["network.port"] == 9123
    assert after["values"]["pricing.models"] == {"fast": {"prompt": 2.0, "completion": 4.0}}
    # 计价变更应立即反映到运行时
    assert ctx.pricing.models["fast"]["prompt"] == 2.0


async def test_settings_reject_invalid_values(client, admin_headers) -> None:
    bad_port = await client.put(
        f"{ADMIN}/settings", json={"values": {"network.port": 99999}}, headers=admin_headers
    )
    assert bad_port.status_code == 400
    bad_unknown = await client.put(
        f"{ADMIN}/settings", json={"values": {"nope.key": 1}}, headers=admin_headers
    )
    assert bad_unknown.status_code == 400
    bad_level = await client.put(
        f"{ADMIN}/settings", json={"values": {"logs.level": "LOUD"}}, headers=admin_headers
    )
    assert bad_level.status_code == 400
    bad_type = await client.put(
        f"{ADMIN}/settings", json={"values": {"network.port": "不是数字"}}, headers=admin_headers
    )
    assert bad_type.status_code == 400


async def test_settings_reset(client, admin_headers) -> None:
    await client.put(
        f"{ADMIN}/settings", json={"values": {"ui.page_size": 50}}, headers=admin_headers
    )
    reset = await client.post(
        f"{ADMIN}/settings/reset", json={"keys": ["ui.page_size"]}, headers=admin_headers
    )
    assert reset.json()["changed"] == ["ui.page_size"]
    assert (await client.get(f"{ADMIN}/settings", headers=admin_headers)).json()["values"]["ui.page_size"] == 20


# --------------------------------------------------------------------------- #
# 统计与余额
# --------------------------------------------------------------------------- #
async def test_stats_logs_and_csv_export(client, admin_headers, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="k")
    headers = auth_header(plaintext)
    for _ in range(2):
        await client.post(
            "/v1/chat/completions",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )
    await ctx.usage.drain()

    stats = (await client.get(f"{ADMIN}/stats?hours=1&bucket=hour", headers=admin_headers)).json()
    assert stats["overview"]["requests"] == 2
    assert stats["overview"]["total_tokens"] == 36
    assert stats["overview"]["avg_latency_ms"] > 0
    assert stats["by_model"][0]["requests"] == 2
    assert stats["by_key"][0]["requests"] == 2
    assert stats["by_channel"][0]["channel_name"] == "c"
    assert len(stats["series"]) == 1
    assert stats["series"][0]["requests"] == 2

    logs = (await client.get(f"{ADMIN}/stats/logs?limit=10", headers=admin_headers)).json()
    assert len(logs["items"]) == 2
    assert logs["live"]["recent"]

    csv_response = await client.get(f"{ADMIN}/stats/export.csv?hours=1", headers=admin_headers)
    assert csv_response.status_code == 200
    assert "attachment" in csv_response.headers["content-disposition"]
    assert "输入tokens" in csv_response.text
    assert csv_response.text.count("\n") >= 3


async def test_balance_overview_and_refresh(client, admin_headers, ctx, mock_upstream) -> None:
    await create_channel(
        ctx,
        name="ds",
        provider_type="deepseek",
        base_url=mock_upstream,
        balance_json_path="",
    )
    await create_channel(ctx, name="plain-openai", provider_type="openai", base_url=mock_upstream)

    overview = (await client.get(f"{ADMIN}/balance", headers=admin_headers)).json()
    by_name = {item["channel_name"]: item for item in overview["items"]}
    assert by_name["ds"]["configured"] is True
    assert by_name["plain-openai"]["configured"] is False
    assert by_name["ds"]["message"] == "尚未查询"

    refreshed = await client.post(f"{ADMIN}/balance/refresh", json={}, headers=admin_headers)
    assert refreshed.status_code == 200
    items = {item["channel_name"]: item for item in refreshed.json()["items"]}
    assert items["ds"]["total"] == 88.5
    assert items["ds"]["currency"] == "CNY"
    assert items["plain-openai"]["supported"] is False

    after = {item["channel_name"]: item for item in (await client.get(f"{ADMIN}/balance", headers=admin_headers)).json()["items"]}
    assert after["ds"]["total"] == 88.5
    assert after["ds"]["fetched_at"] is not None

    ds_id = next(item["channel_id"] for item in after.values() if item["channel_name"] == "ds")
    history = await client.get(f"{ADMIN}/balance/{ds_id}/history", headers=admin_headers)
    assert len(history.json()["items"]) == 1


async def test_health_endpoint(client, admin_headers, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    response = await client.get(f"{ADMIN}/health", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["channels"][0]["name"] == "c"
    assert "ratelimit" in payload and "live" in payload


async def test_live_snapshot_endpoint(client, admin_headers) -> None:
    response = await client.get(f"{ADMIN}/live", headers=admin_headers)
    payload = response.json()
    assert payload["type"] == "live"
    assert payload["active"] == []
    assert payload["stats"]["active"] == 0
    assert "ratelimit" in payload


# --------------------------------------------------------------------------- #
# 运维
# --------------------------------------------------------------------------- #
async def test_rotate_admin_token_invalidates_old_one(remote_client, admin_headers, ctx) -> None:
    old_token = ctx.admin_token
    response = await remote_client.post(f"{ADMIN}/system/rotate-token", headers=admin_headers)
    assert response.status_code == 200
    new_token = response.json()["admin_token"]
    assert new_token != old_token
    assert ctx.admin_token == new_token
    # 未登录的远程客户端：老令牌立即失效，新令牌可用
    assert (
        await remote_client.get(f"{ADMIN}/system", headers={"X-Admin-Token": old_token})
    ).status_code == 401
    assert (
        await remote_client.get(f"{ADMIN}/system", headers={"X-Admin-Token": new_token})
    ).status_code == 200


async def test_backup_export_hides_secrets(client, admin_headers, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream, api_key="sk-secret-value")
    response = await client.post(f"{ADMIN}/system/backup", headers=admin_headers)
    body = response.json()
    assert body["channels"][0]["name"] == "c"
    assert "sk-secret-value" not in response.text
    assert body["keys"] == []


async def test_restart_returns_guidance_without_host_manager(client, admin_headers) -> None:
    response = await client.post(f"{ADMIN}/system/restart", headers=admin_headers)
    assert response.json()["ok"] is False
    assert "重启" in response.json()["message"]


async def test_openapi_and_readyz(client) -> None:
    assert (await client.get("/readyz")).json()["ready"] is True
    spec = (await client.get("/openapi.json")).json()
    assert "/v1/chat/completions" in spec["paths"]
    assert f"{ADMIN}/keys" in spec["paths"]


async def test_settings_survive_restart(client, admin_headers, app_context, tmp_path) -> None:
    """设置必须真的落库：重启后（新的 AppContext / 新的事件循环）仍然生效。

    这条曾经是盲区——只验证同进程读回（走内存缓存）会漏掉「根本没写库」。
    """
    from airelay.context import AppContext
    from airelay.security import load_or_create_secrets
    from airelay.settings import SettingsService

    await client.put(
        f"{ADMIN}/settings",
        json={"values": {"network.port": 9411, "gateway.max_retries": 3, "ui.theme": "light"}},
        headers=admin_headers,
    )
    await app_context.shutdown()

    fresh_settings = SettingsService()
    restarted = AppContext(
        paths=app_context.paths,
        settings=fresh_settings,
        secrets=load_or_create_secrets(app_context.paths.data_dir),
        mode="server",
    )
    await restarted.startup()
    try:
        assert fresh_settings.get_int("network.port", 8000) == 9411
        assert fresh_settings.get_int("gateway.max_retries", 1) == 3
        assert fresh_settings.get_str("ui.theme", "dark") == "light"
        assert restarted.port == 9411
    finally:
        await restarted.shutdown()


async def test_put_same_value_as_override_still_persists(ctx) -> None:
    """命令行覆盖（--port 等）过的项，在控制台保存同值也必须写库，才能被「锁定」。"""
    from airelay.settings import SettingsService

    settings = SettingsService()
    settings.set_overrides({"network.port": 9412})
    assert settings.get_int("network.port", 8000) == 9412

    async with ctx.session_factory() as session:
        await settings.load(session)
        assert settings.get_int("network.port", 8000) == 9412  # 覆盖仍然生效
        changes = await settings.update(session, {"network.port": 9412})
    assert [change.key for change in changes] == ["network.port"]
    assert settings.overrides == {}

    reloaded = SettingsService()
    async with ctx.session_factory() as session:
        await reloaded.load(session)
    assert reloaded.get_int("network.port", 8000) == 9412


async def test_rotate_key_value_keeps_config_and_usage(client, admin_headers, ctx, mock_upstream) -> None:
    """重新生成密钥值：换秘密字符串，旧值立即失效，配置与统计保留。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    public, old_plaintext = await create_key(
        ctx,
        name="待轮换",
        quota_limit=2_000_000,
        model_allowed=["fast"],
        rpm_limit=30,
    )
    key_id = public["key_id"]

    # 先用旧密钥跑一次，制造用量
    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(old_plaintext),
    )
    assert ok.status_code == 200
    await ctx.usage.drain()

    rotated = await client.post(f"{ADMIN}/keys/{key_id}/rotate", headers=admin_headers)
    assert rotated.status_code == 200
    body = rotated.json()
    new_plaintext = body["plaintext"]
    assert new_plaintext != old_plaintext
    assert new_plaintext.startswith("sk-relay-")
    # 同一把密钥的身份与配置不变
    assert body["key"]["key_id"] == key_id
    assert body["key"]["name"] == "待轮换"
    assert body["key"]["quota_limit"] == 2_000_000
    assert body["key"]["rpm_limit"] == 30
    assert body["key"]["model_allowed"] == ["fast"]
    assert body["key"]["prefix"] != public["prefix"]
    assert "失效" in body["notice"]

    # 旧明文立即失效
    denied = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(old_plaintext),
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "INVALID_API_KEY"

    # 新明文可用，且沿用同一套配置与账本
    allowed = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(new_plaintext),
    )
    assert allowed.status_code == 200
    await ctx.usage.drain()  # 用量异步落库，读统计前先等在途写入完成
    detail = (await client.get(f"{ADMIN}/keys/{key_id}", headers=admin_headers)).json()
    assert detail["total_requests"] == 2
    assert detail["model_allowed"] == ["fast"]


async def test_rotate_unknown_key_returns_404(client, admin_headers) -> None:
    response = await client.post(f"{ADMIN}/keys/key_not_exists/rotate", headers=admin_headers)
    assert response.status_code == 404
