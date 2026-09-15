"""管理面 WebSocket：实时会话推送（方案 8 节）。"""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..errors import RelayError
from .deps import ADMIN_COOKIE, ADMIN_HEADER, client_ip_of, is_loopback

log = logging.getLogger(__name__)

router = APIRouter(tags=["实时会话"])

WS_PATH = "/api/admin/live/ws"


async def _authorize(websocket: WebSocket, ctx) -> bool:
    token = websocket.query_params.get("admin_token") or websocket.headers.get(ADMIN_HEADER) or ""
    if token and hmac.compare_digest(token, ctx.admin_token):
        return True
    cookie = websocket.cookies.get(ADMIN_COOKIE) or ""
    if cookie and ctx.signer.verify(cookie) is not None:
        return True
    host = client_ip_of(websocket, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False))
    if is_loopback(host):
        return True
    return not ctx.settings.get_bool("security.require_admin_token_remote", True)


@router.websocket(WS_PATH)
async def live_socket(websocket: WebSocket) -> None:
    ctx = websocket.app.state.ctx
    if ctx is None or not await _authorize(websocket, ctx):  # pragma: no cover - 未授权
        await websocket.close(code=4401, reason="unauthorized")
        return

    await websocket.accept()
    queue = ctx.live.subscribe()
    send_lock = asyncio.Lock()

    def snapshot() -> dict:
        return ctx.live.snapshot(
            stale_seconds=ctx.settings.get_int("monitoring.stale_seconds", 300),
            recent_limit=ctx.settings.get_int("monitoring.recent_limit", 50),
        )

    async def pump() -> None:
        interval = max(0.2, ctx.settings.get_int("monitoring.push_interval_ms", 1000) / 1000.0)
        # 首帧立刻给完整快照，后续按节流间隔发
        async with send_lock:
            await websocket.send_json(snapshot())
        while True:
            await asyncio.sleep(interval)
            if not ctx.settings.get_bool("monitoring.push_enabled", True):
                continue
            async with send_lock:
                await websocket.send_json(snapshot())

    async def listen() -> None:
        while True:
            message = await websocket.receive_text()
            text = (message or "").strip().lower()
            if text == "ping":
                async with send_lock:
                    await websocket.send_json({"type": "pong"})
            elif text in {"snapshot", "refresh"}:
                async with send_lock:
                    await websocket.send_json(snapshot())

    pump_task = asyncio.create_task(pump())
    listen_task = asyncio.create_task(listen())
    try:
        done, pending = await asyncio.wait(
            {pump_task, listen_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                raise exc
    except WebSocketDisconnect:
        pass
    except RelayError:
        pass
    except Exception:  # pragma: no cover
        log.debug("实时会话连接异常结束", exc_info=True)
    finally:
        for task in (pump_task, listen_task):
            task.cancel()
        ctx.live.unsubscribe(queue)
        try:
            await websocket.close()
        except Exception:
            pass
