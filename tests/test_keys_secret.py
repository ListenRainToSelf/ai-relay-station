"""本地密钥的「平台式管理」：明文加密存库、可反复取回，以及关闭该能力时的降级。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from conftest import auth_header, create_channel, create_key

pytestmark = pytest.mark.anyio

ADMIN = "/api/admin"


async def test_plaintext_is_retrievable_again_and_again(client, admin_headers, ctx, mock_upstream) -> None:
    """创建之后明文可以反复取回——这才是「密钥管理」，不是一次性展示。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    public, plaintext = await create_key(ctx, name="可反复取回")
    assert public["has_secret"] is True

    for round_no in range(3):
        response = await client.get(f"{ADMIN}/keys/{public['key_id']}/secret", headers=admin_headers)
        assert response.status_code == 200, f"第 {round_no + 1} 次取回失败"
        body = response.json()
        assert body["plaintext"] == plaintext
        assert body["name"] == "可反复取回"
        assert body["key_id"] == public["key_id"]

    # 取回的明文确实能用来调用
    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(plaintext),
    )
    assert ok.status_code == 200


async def test_secret_endpoint_requires_admin_auth(remote_client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    public, _ = await create_key(ctx, name="受保护")
    denied = await remote_client.get(f"{ADMIN}/keys/{public['key_id']}/secret")
    assert denied.status_code == 401
    allowed = await remote_client.get(
        f"{ADMIN}/keys/{public['key_id']}/secret", headers={"X-Admin-Token": ctx.admin_token}
    )
    assert allowed.status_code == 200


async def test_list_does_not_leak_plaintext(client, admin_headers, ctx, mock_upstream) -> None:
    """列表接口不能顺手把明文带出来，只能单独取。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(ctx, name="不泄漏")
    listing = await client.get(f"{ADMIN}/keys", headers=admin_headers)
    assert plaintext not in listing.text
    detail = await client.get(f"{ADMIN}/keys", headers=admin_headers)
    assert all("plaintext" not in item for item in detail.json()["items"])


async def test_rotate_replaces_stored_plaintext(client, admin_headers, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    public, old_plaintext = await create_key(ctx, name="轮换后仍可取回")
    rotated = await client.post(f"{ADMIN}/keys/{public['key_id']}/rotate", headers=admin_headers)
    new_plaintext = rotated.json()["plaintext"]
    assert new_plaintext != old_plaintext

    secret = (await client.get(f"{ADMIN}/keys/{public['key_id']}/secret", headers=admin_headers)).json()
    assert secret["plaintext"] == new_plaintext

    old_call = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(old_plaintext),
    )
    assert old_call.status_code == 401
    new_call = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(new_plaintext),
    )
    assert new_call.status_code == 200


async def test_legacy_key_without_ciphertext_returns_actionable_409(client, admin_headers, ctx) -> None:
    """老数据（加密存储启用前创建）取不到明文时，要给出可执行的提示而不是 500。"""
    public, plaintext = await create_key(ctx, name="老数据")
    async with ctx.session_factory() as session:
        await session.execute(
            text("UPDATE api_keys SET key_enc = '' WHERE key_id = :key_id"), {"key_id": public["key_id"]}
        )
        await session.commit()

    listing = (await client.get(f"{ADMIN}/keys", headers=admin_headers)).json()["items"]
    assert listing[0]["has_secret"] is False

    response = await client.get(f"{ADMIN}/keys/{public['key_id']}/secret", headers=admin_headers)
    assert response.status_code == 409
    assert "重新生成" in response.json()["message"]
    assert response.json()["details"]["hint"]

    # 但鉴权不受影响：哈希还在
    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(plaintext),
    )
    assert ok.status_code in (200, 503)  # 无渠道时也是 503，总之不是 401


async def test_keep_plaintext_disabled_falls_back_to_hash_only(client, admin_headers, ctx, mock_upstream) -> None:
    """关掉「本地密钥可再次查看」后，新密钥只存哈希：鉴权照常，但取不回明文。"""
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    await client.put(
        f"{ADMIN}/settings",
        json={"values": {"security.keep_key_plaintext": False}},
        headers=admin_headers,
    )
    public, plaintext = await create_key(ctx, name="只存哈希")
    assert public["has_secret"] is False

    response = await client.get(f"{ADMIN}/keys/{public['key_id']}/secret", headers=admin_headers)
    assert response.status_code == 409

    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(plaintext),
    )
    assert ok.status_code == 200

    # 老密钥（已有密文的）不受影响
    await client.put(
        f"{ADMIN}/settings",
        json={"values": {"security.keep_key_plaintext": True}},
        headers=admin_headers,
    )
    again, again_plaintext = await create_key(ctx, name="恢复后可取回")
    assert again["has_secret"] is True
    secret = (await client.get(f"{ADMIN}/keys/{again['key_id']}/secret", headers=admin_headers)).json()
    assert secret["plaintext"] == again_plaintext


async def test_light_migration_adds_missing_column(tmp_path) -> None:
    """老库升级：api_keys 缺 key_enc 时，init_db 要能自动补上。"""
    from airelay.db import build_engine, dispose, init_db

    engine = build_engine(tmp_path / "legacy.db")
    try:
        await init_db(engine)
        async with engine.begin() as conn:
            columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(api_keys)"))).fetchall()}
            assert "key_enc" in columns
            # 模拟升级前的老表结构
            await conn.execute(text("ALTER TABLE api_keys DROP COLUMN key_enc"))
            dropped = {row[1] for row in (await conn.execute(text("PRAGMA table_info(api_keys)"))).fetchall()}
            assert "key_enc" not in dropped
        await init_db(engine)  # 再初始化一次应当把列补回来
        async with engine.connect() as conn:
            restored = {row[1] for row in (await conn.execute(text("PRAGMA table_info(api_keys)"))).fetchall()}
        assert "key_enc" in restored
    finally:
        await dispose(engine)
