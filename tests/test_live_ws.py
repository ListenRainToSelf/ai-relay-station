"""实时会话与 WebSocket 推送测试（跑在真实端口上）。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets

from conftest import auth_header, create_channel, create_key, free_port
from airelay.host import AppHost

pytestmark = pytest.mark.anyio


async def poll_http(url: str, *, timeout: float = 20.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient() as probe:
        while asyncio.get_event_loop().time() < deadline:
            try:
                response = await probe.get(url, timeout=1.0)
                if response.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    return False


async def start_host(app_context, port: int) -> tuple[AppHost, str]:
    host = AppHost(app_context, host="127.0.0.1", port=port)
    if not host.start(wait=True):
        raise RuntimeError(f"网关启动失败：{host.bind_error}")
    base_url = f"http://127.0.0.1:{port}"
    if not await poll_http(f"{base_url}/healthz"):
        host.stop()
        raise RuntimeError("网关未在预期时间内就绪")
    return host, base_url


async def test_websocket_streams_active_session(
    app_context, mock_upstream, mock_state
) -> None:
    # 先在当前事件循环里把渠道与密钥准备好，再交给宿主线程重启服务
    await app_context.startup()
    await create_channel(app_context, name="live-ch", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(app_context, name="live-key")
    await app_context.shutdown()

    port = free_port()
    host, base_url = await start_host(app_context, port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/api/admin/live/ws") as socket:
            first = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            assert first["type"] == "live"
            assert first["active"] == []

            mock_state.STATE["stream_delay"] = 0.25

            async def consume_stream() -> str:
                collected = ""
                async with httpx.AsyncClient(timeout=30.0) as http:
                    async with http.stream(
                        "POST",
                        f"{base_url}/v1/chat/completions",
                        json={
                            "model": "fast",
                            "messages": [{"role": "user", "content": "你好"}],
                            "stream": True,
                        },
                        headers=auth_header(plaintext),
                    ) as response:
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

            task = asyncio.create_task(consume_stream())
            active = None
            for _ in range(40):
                payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                actives = payload.get("active") or []
                if actives:
                    active = actives[0]
                    break
            assert active is not None, "应当在推送里看到进行中的会话"
            assert active["model"] == "fast"
            assert active["key_name"] == "live-key"
            assert active["channel_name"] == "live-ch"
            assert active["provider_type"] == "openai"
            assert active["stream"] is True
            assert active["status"] in ("running", "stalled")
            assert active["elapsed_ms"] >= 0

            text = await asyncio.wait_for(task, timeout=30)
            assert text == mock_state.REPLY_TEXT

            # 结束后会话从活跃列表移出，进入最近完成列表
            finished = None
            for _ in range(40):
                payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if payload.get("active") == [] and payload.get("recent"):
                    finished = payload["recent"][0]
                    break
            assert finished is not None
            assert finished["status"] == "ok"
            assert finished["total_tokens"] > 0
            assert finished["speed_tok_s"] > 0
    finally:
        host.stop()


async def test_admin_live_http_snapshot_over_real_server(app_context, mock_upstream) -> None:
    await app_context.startup()
    await create_channel(app_context, name="c", provider_type="openai", base_url=mock_upstream)
    _, plaintext = await create_key(app_context, name="k")
    await app_context.shutdown()

    host, base_url = await start_host(app_context, free_port())
    try:
        headers = {"Authorization": f"Bearer {plaintext}"}
        async with httpx.AsyncClient(timeout=30.0) as http:
            for _ in range(3):
                response = await http.post(
                    f"{base_url}/v1/chat/completions",
                    json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
                    headers=headers,
                )
                assert response.status_code == 200
            snapshot = await http.get(f"{base_url}/api/admin/live")
            payload = snapshot.json()
            assert payload["stats"]["started"] == 3
            assert len(payload["recent"]) == 3
            assert payload["active"] == []

            # 用量是异步落库的，轮询等一下（最多 5 秒）
            items: list = []
            for _ in range(50):
                logs = await http.get(f"{base_url}/api/admin/stats/logs?limit=10")
                items = logs.json()["items"]
                if len(items) >= 3:
                    break
                await asyncio.sleep(0.1)
            assert len(items) == 3
            assert all(item["status"] == "ok" for item in items)
    finally:
        host.stop()
