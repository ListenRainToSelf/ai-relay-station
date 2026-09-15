"""管理面（/api/admin/*）：供 WebView 控制台使用的读写接口。

安全模型（方案 6.2）：默认只绑定环回地址；远程访问必须携带管理员令牌，
登录后换发 HttpOnly 会话 Cookie，密钥池与余额不外泄。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select

from .. import schemas
from ..adapters import provider_metadata
from ..errors import ErrorCode, RelayError, bad_request, not_found
from ..models import ApiKey, Channel, ModelMap
from ..security import mask_key, rotate_admin_token
from ..services.keys import KeyService
from ..services.supervisor import port_of_url
from ..version import APP_NAME, __version__
from .deps import (
    ADMIN_COOKIE,
    can_auto_login,
    client_ip_of,
    db_session,
    issue_session_cookie,
    require_admin,
)

log = logging.getLogger(__name__)

# 会话引导接口不带鉴权依赖，其余全部要求已登录
session_router = APIRouter(prefix="/api/admin", tags=["管理面 · 会话"])
router = APIRouter(prefix="/api/admin", tags=["管理面"], dependencies=[Depends(require_admin)])


# =========================================================================== #
# 会话
# =========================================================================== #
@session_router.get("/session")
async def session_state(request: Request, response: Response):
    ctx = request.app.state.ctx
    cookie = request.cookies.get(ADMIN_COOKIE) or ""
    existing = ctx.signer.verify(cookie) if cookie else None
    auto = can_auto_login(request, ctx)
    host = client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False))

    if existing is not None:
        return {"authenticated": True, "client": existing.client or host, "auto": True}
    if auto:
        token, ttl = issue_session_cookie(request, ctx, client=host or "loopback")
        response.set_cookie(
            ADMIN_COOKIE, token, max_age=ttl, httponly=True, samesite="strict", path="/"
        )
        return {"authenticated": True, "client": host or "loopback", "auto": True}
    return {"authenticated": False, "requires_token": True, "client": host}


@session_router.post("/session")
async def login(payload: schemas.LoginPayload, request: Request, response: Response):
    ctx = request.app.state.ctx
    import hmac

    if not hmac.compare_digest(payload.token.strip(), ctx.admin_token):
        return JSONResponse(
            status_code=401,
            content={"authenticated": False, "code": "UNAUTHORIZED", "message": "管理员令牌不正确"},
        )
    host = client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False))
    token, ttl = issue_session_cookie(request, ctx, client=f"token@{host}")
    response.set_cookie(ADMIN_COOKIE, token, max_age=ttl, httponly=True, samesite="strict", path="/")
    return {"authenticated": True, "client": host, "auto": False}


@session_router.delete("/session")
async def logout(response: Response):
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return {"authenticated": False}


# =========================================================================== #
# 系统
# =========================================================================== #
@router.get("/system")
async def system_info(request: Request, session: Any = Depends(db_session)):
    from sqlalchemy import func, select

    from ..models import ApiKey, Channel, ModelMap

    ctx = request.app.state.ctx
    description = ctx.describe()
    description.update(
        {
            "counts": {
                "channels": int((await session.execute(select(func.count()).select_from(Channel))).scalar_one()),
                "keys": int((await session.execute(select(func.count()).select_from(ApiKey))).scalar_one()),
                "model_maps": int((await session.execute(select(func.count()).select_from(ModelMap))).scalar_one()),
                "active_sessions": ctx.live.active_count,
                "ws_subscribers": ctx.live.subscriber_count,
            },
            "providers": provider_metadata(),
            "ratelimit": ctx.ratelimiter.usage_snapshot(),
            "health": ctx.router.health_snapshot(),
            "maintenance": ctx.maintenance.last_run,
        }
    )
    return description


@router.post("/system/rotate-token")
async def rotate_token(request: Request):
    ctx = request.app.state.ctx
    ctx.secrets = type(ctx.secrets)(  # 保持数据类结构不变，仅替换令牌
        pepper=ctx.secrets.pepper,
        master_key=ctx.secrets.master_key,
        admin_token=rotate_admin_token(ctx.paths.data_dir),
        session_secret=ctx.secrets.session_secret,
    )
    log.warning("管理员令牌已轮换，旧的令牌立即失效（已登录会话仍有效直到过期）")
    return {"ok": True, "admin_token": ctx.admin_token}


@router.post("/system/restart")
async def restart_service(request: Request):
    """重新绑定监听地址/端口（托盘/进程内宿主管辖时可用）。"""
    ctx = request.app.state.ctx
    if ctx.restart_callback is None:
        return {
            "ok": False,
            "message": "当前由外部进程托管（systemd / Docker / 命令行），请重启服务使新的 IP 与端口生效",
        }
    await ctx.restart_signal()
    return {"ok": True, "message": f"已按新的监听参数 {ctx.host}:{ctx.port} 重新绑定"}


@router.post("/system/backup")
async def backup(request: Request):
    """导出配置快照（不含上游密钥明文，含本地密钥哈希与渠道配置）。"""
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        from sqlalchemy import select

        from ..models import ApiKey, Channel, ModelMap, Setting

        settings_rows = (await session.execute(select(Setting))).scalars().all()
        channels = (await session.execute(select(Channel))).scalars().all()
        keys = (await session.execute(select(ApiKey))).scalars().all()
        maps = (await session.execute(select(ModelMap))).scalars().all()
    return {
        "version": __version__,
        "exported_at": ctx.describe()["started_at"],
        "settings": {row.key: row.value for row in settings_rows},
        "channels": [ctx.channels.to_public(row) for row in channels],
        "keys": [KeyService.to_public(row) for row in keys],
        "model_map": [ctx.mapping.to_public(row) for row in maps],
        "notice": "本快照不含上游 API Key 明文与本地密钥明文，仅用于核对配置。",
    }


# =========================================================================== #
# 密钥
# =========================================================================== #
@router.get("/keys")
async def list_keys(
    request: Request,
    session: Any = Depends(db_session),
    search: str = "",
    status: str = "",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    ctx = request.app.state.ctx
    items, total = await ctx.keys.list_keys(
        session, search=search, status=status, limit=limit, offset=offset
    )
    usage = ctx.ratelimiter.usage_snapshot([item["key_id"] for item in items])
    for item in items:
        item["ratelimit"] = usage["keys"].get(item["key_id"], {"rpm_used": 0, "tpm_used": 0})
    return {"items": items, "total": total}


@router.post("/keys")
async def create_key(
    payload: schemas.KeyCreate, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    defaults = {
        "rpm": ctx.settings.get_int("ratelimit.default_key_rpm", 0),
        "tpm": ctx.settings.get_int("ratelimit.default_key_tpm", 0),
    }
    record, plaintext = await ctx.keys.create(session, payload.model_dump(), defaults=defaults)
    return {
        "key": ctx.keys.to_public(record),
        "plaintext": plaintext,
        "notice": "请立即复制保存：明文密钥仅此一次展示，之后只保留前缀与掩码。",
    }


@router.get("/keys/{key_id}")
async def get_key(key_id: str, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    record = await ctx.keys.get(session, key_id)
    detail = ctx.keys.to_public(record)
    detail["usage"] = await ctx.keys.usage_summary(session, key_id)
    detail["totals"] = await ctx.usage.totals_for_key(key_id)
    detail["ratelimit"] = ctx.ratelimiter.usage_snapshot([key_id])["keys"].get(key_id, {})
    return detail


@router.put("/keys/{key_id}")
async def update_key(
    key_id: str, payload: schemas.KeyUpdate, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    record = await ctx.keys.update(session, key_id, values)
    if values.get("reset_used"):
        ctx.ratelimiter.reset(key_id)
    return ctx.keys.to_public(record)


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    await ctx.keys.delete(session, key_id)
    ctx.ratelimiter.reset(key_id)
    return {"ok": True}


@router.post("/keys/{key_id}/reset-usage")
async def reset_key_usage(key_id: str, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    record = await ctx.keys.update(session, key_id, {"reset_used": True})
    ctx.ratelimiter.reset(key_id)
    return {"ok": True, "key": ctx.keys.to_public(record)}


@router.post("/keys/{key_id}/rotate")
async def rotate_key(key_id: str, request: Request, session: Any = Depends(db_session)):
    """重新生成密钥值（旧明文立即失效），配额 / 限速 / 模型授权 / 有效期保持不变。"""
    ctx = request.app.state.ctx
    record, plaintext = await ctx.keys.rotate(session, key_id)
    return {
        "key": ctx.keys.to_public(record),
        "plaintext": plaintext,
        "notice": "旧密钥值已立即失效，请把新值更新到客户端。配置与统计保持不变。",
    }


@router.get("/keys/{key_id}/secret")
async def reveal_key_secret(key_id: str, request: Request, session: Any = Depends(db_session)):
    """取回密钥明文（本地密钥管理）。

    明文是加密存在库里的，所以创建之后可以随时再查看与复制。这条路径会记审计日志：
    管理面一旦可用就等于能拿到所有本地密钥，所以本机部署要守好环回绑定与管理员令牌。
    """
    ctx = request.app.state.ctx
    record, plaintext = await ctx.keys.get_secret(session, key_id)
    client = client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False))
    if not plaintext:
        log.warning("读取密钥明文失败：%s（无密文或主密钥已更换）来自 %s", record.key_id, client)
        raise RelayError(
            ErrorCode.NOT_FOUND,
            "该密钥没有可取的明文（可能是「本地密钥可再次查看」关闭时创建的，或数据目录的主密钥已更换）；"
            "点「重新生成密钥值」即可拿到新的一把，配额与统计会保留",
            status=409,
            details={"hint": "点「重新生成密钥值」即可拿到一把新的，配置与统计会保留"},
        )
    log.warning("管理员读取了密钥明文：%s（%s）来自 %s", record.name, mask_key(record.prefix), client)
    return {
        "key_id": record.key_id,
        "name": record.name,
        "masked": mask_key(record.prefix),
        "prefix": record.prefix,
        "plaintext": plaintext,
        "created_at": record.created_at.isoformat() + "Z" if record.created_at else None,
        "updated_at": record.updated_at.isoformat() + "Z" if record.updated_at else None,
    }


# =========================================================================== #
# 渠道
# =========================================================================== #
@router.get("/channels")
async def list_channels(
    request: Request,
    session: Any = Depends(db_session),
    search: str = "",
    status: str = "",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    ctx = request.app.state.ctx
    items, total = await ctx.channels.list_channels(
        session, search=search, status=status, limit=limit, offset=offset
    )
    health = ctx.router.health_snapshot()
    for item in items:
        item["health"] = health.get(
            item["channel_id"],
            {"cooling": False, "cooldown_remaining": 0, "failures": 0, "successes": 0},
        )
        item["api_key_hint"] = ctx.channels.masked_key_of(
            await ctx.channels.get(session, item["channel_id"])
        )
    return {"items": items, "total": total, "providers": provider_metadata()}


@router.post("/channels")
async def create_channel(
    payload: schemas.ChannelCreate, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    record = await ctx.channels.create(session, payload.model_dump())
    return ctx.channels.to_public(record)


@router.get("/channels/{channel_id}")
async def get_channel(channel_id: str, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    record = await ctx.channels.get(session, channel_id)
    detail = ctx.channels.to_public(record)
    detail["api_key_hint"] = ctx.channels.masked_key_of(record)
    detail["health"] = ctx.router.health(channel_id).to_dict()
    return detail


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: str,
    payload: schemas.ChannelUpdate,
    request: Request,
    session: Any = Depends(db_session),
):
    ctx = request.app.state.ctx
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    record = await ctx.channels.update(session, channel_id, values)
    detail = ctx.channels.to_public(record)
    detail["api_key_hint"] = ctx.channels.masked_key_of(record)
    return detail


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    await ctx.channels.delete(session, channel_id)
    ctx.router.clear_cooldown(channel_id)
    return {"ok": True}


@router.post("/channels/{channel_id}/probe")
async def probe_channel(
    channel_id: str,
    payload: schemas.ProbePayload,
    request: Request,
    session: Any = Depends(db_session),
):
    ctx = request.app.state.ctx
    record = await ctx.channels.get(session, channel_id)
    if ctx.http is None:
        raise bad_request("网关尚未完成初始化")
    result = await ctx.channels.probe(
        record, model=payload.model, client=ctx.http, max_tokens=payload.max_tokens
    )
    return result


@router.get("/channels/{channel_id}/models")
async def channel_models(channel_id: str, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    record = await ctx.channels.get(session, channel_id)
    if ctx.http is None:
        raise bad_request("网关尚未完成初始化")
    models = await ctx.channels.fetch_models(record, client=ctx.http)
    return {"items": models, "total": len(models)}


@router.post("/channels/{channel_id}/reset-cooldown")
async def reset_channel_cooldown(channel_id: str, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    await ctx.channels.get(session, channel_id)
    ctx.router.clear_cooldown(channel_id)
    return {"ok": True, "health": ctx.router.health(channel_id).to_dict()}


# =========================================================================== #
# 模型别名
# =========================================================================== #
@router.get("/models/map")
async def list_model_map(request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    return {"items": await ctx.mapping.list_maps(session)}


@router.post("/models/map")
async def create_model_map(
    payload: schemas.ModelMapCreate, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    record = await ctx.mapping.create(session, payload.model_dump())
    return ctx.mapping.to_public(record)


@router.put("/models/map/{map_id}")
async def update_model_map(
    map_id: int,
    payload: schemas.ModelMapUpdate,
    request: Request,
    session: Any = Depends(db_session),
):
    ctx = request.app.state.ctx
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    record = await ctx.mapping.update(session, map_id, values)
    return ctx.mapping.to_public(record)


@router.delete("/models/map/{map_id}")
async def delete_model_map(map_id: int, request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    await ctx.mapping.delete(session, map_id)
    return {"ok": True}


@router.post("/models/map/import")
async def import_model_map(
    payload: schemas.ModelMapImport, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    if payload.replace:
        for item in await ctx.mapping.list_maps(session):
            await ctx.mapping.delete(session, item["id"])
    result = await ctx.mapping.bulk_import(
        session, [entry.model_dump() for entry in payload.entries]
    )
    return {"ok": True, **result, "total": await ctx.mapping.count(session)}


@router.get("/models/resolve")
async def resolve_model(request: Request, model: str = Query(...), session: Any = Depends(db_session)):
    """试算：给定模型名，看会解析到哪个上游模型、命中哪些渠道。"""
    ctx = request.app.state.ctx
    resolved = ctx.mapping.resolve(model)
    channels = await ctx.channels.enabled_channels(session)
    plan = await ctx.router.plan(session, resolved=resolved, channels=channels, key_id="")
    health = ctx.router.health_snapshot()
    return {
        "resolved": resolved.to_dict(),
        "candidates": [
            {
                "channel_id": channel.channel_id,
                "name": channel.name,
                "provider_type": channel.provider_type,
                "priority": channel.priority,
                "weight": channel.weight,
                "health": health.get(channel.channel_id, {}),
            }
            for channel in plan.candidates
        ],
        "reason": plan.reason,
    }


# =========================================================================== #
# 统计
# =========================================================================== #
@router.get("/stats")
async def stats(
    request: Request,
    hours: float = Query(24.0, gt=0, le=24 * 365),
    bucket: str = Query("hour", pattern="^(hour|day)$"),
    key_id: str = Query("", description="只看某个本地密钥"),
    model: str = Query("", description="只看某个模型"),
    group_by: str = Query("", pattern="^(|model)$", description="model = 额外返回按模型拆分的多条序列"),
):
    """统计总览与序列。

    所有聚合都吃同一套筛选条件（时间窗 / 本地密钥 / 模型），
    这样控制台上的「筛选某个 API Key + 某个模型的折线图」就是同一次请求的结果。
    """
    ctx = request.app.state.ctx
    payload: dict[str, Any] = {
        "overview": await ctx.usage.overview(hours=hours, key_id=key_id, model=model),
        "series": await ctx.usage.series(hours=hours, bucket=bucket, key_id=key_id, model=model),
        "by_model": await ctx.usage.by_model(hours=hours, key_id=key_id, model=model),
        "by_key": await ctx.usage.by_key(hours=hours, key_id=key_id, model=model),
        "by_channel": await ctx.usage.by_channel(hours=hours, key_id=key_id, model=model),
        "filters": {"hours": hours, "bucket": bucket, "key_id": key_id, "model": model, "group_by": group_by},
        "options": await ctx.usage.options(hours=hours),
        "pricing": {
            "currency": ctx.pricing.currency,
            "models": ctx.pricing.models,
            "default": ctx.pricing.default,
        },
    }
    if group_by == "model":
        payload["series_by_model"] = await ctx.usage.series_by_model(
            hours=hours, bucket=bucket, key_id=key_id
        )
    return payload


@router.get("/stats/logs")
async def usage_logs(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    key_id: str = Query("", description="只看某个本地密钥"),
    status: str = "",
    model: str = Query("", description="只看某个模型"),
):
    ctx = request.app.state.ctx
    return {
        "items": await ctx.usage.recent(limit=limit, key_id=key_id, status=status, model=model),
        "live": ctx.live.snapshot(
            stale_seconds=ctx.settings.get_int("monitoring.stale_seconds", 300),
            recent_limit=ctx.settings.get_int("monitoring.recent_limit", 50),
        ),
    }


@router.get("/stats/export.csv")
async def export_csv(
    request: Request, hours: float = Query(24.0, gt=0, le=24 * 365), limit: int = Query(5000, ge=1, le=100000)
):
    ctx = request.app.state.ctx
    rows = await ctx.usage.recent(limit=limit)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "时间",
            "请求ID",
            "密钥",
            "渠道",
            "协议",
            "模型",
            "上游模型",
            "输入tokens",
            "输出tokens",
            "总tokens",
            "耗时ms",
            "首字ms",
            "速度tok/s",
            "流式",
            "重试次数",
            "状态",
            "错误码",
            "费用µ$",
            "客户端IP",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["ts"],
                row["request_id"],
                row["key_name"] or row["key_prefix"],
                row["channel_name"],
                row["provider_type"],
                row["model"],
                row["upstream_model"],
                row["prompt_tokens"],
                row["completion_tokens"],
                row["total_tokens"],
                row["latency_ms"],
                row["first_token_ms"],
                row["speed_tok_s"],
                "是" if row["stream"] else "否",
                row["attempts"],
                row["status"],
                row["error_code"],
                row["cost_units"],
                row["client_ip"],
            ]
        )
    filename = f"airelay-usage-{ctx.started_at:%Y%m%d}.csv"
    return PlainTextResponse(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =========================================================================== #
# 实时会话
# =========================================================================== #
@router.get("/live")
async def live_snapshot(request: Request):
    ctx = request.app.state.ctx
    payload = ctx.live.snapshot(
        stale_seconds=ctx.settings.get_int("monitoring.stale_seconds", 300),
        recent_limit=ctx.settings.get_int("monitoring.recent_limit", 50),
    )
    payload["ratelimit"] = ctx.ratelimiter.usage_snapshot()
    return payload


# =========================================================================== #
# 本地服务托管（本机推理进程的启停与自动重启）
# =========================================================================== #
async def _channel_for(session: Any, ctx: Any, channel_id: str):
    return await ctx.channels.get(session, channel_id)


@router.get("/services")
async def list_services(request: Request, session: Any = Depends(db_session)):
    """列出被托管的本地服务及其运行时状态（含最近事件）。"""
    ctx = request.app.state.ctx
    channels = (await session.execute(select(Channel))).scalars().all()
    managed = [c for c in channels if c.lifecycle_config()["enabled"]]
    states = {item["channel_id"]: item for item in (ctx.services.snapshot() if ctx.services else [])}
    items = []
    for channel in managed:
        config = channel.lifecycle_config()
        state = states.get(channel.channel_id) or {
            "status": "unknown", "healthy": False, "detail": "尚未探活",
            "managed": False, "pid": None, "restarts": 0, "consecutive_failures": 0,
            "latency_ms": 0.0, "uptime_seconds": 0, "retry_in_seconds": 0, "events": [],
            "last_check_at": None, "last_healthy_at": None, "started_at": None,
        }
        items.append(
            {
                "channel_id": channel.channel_id,
                "channel_name": channel.name,
                "provider_type": channel.provider_type,
                "base_url": channel.base_url,
                "models": channel.model_patterns(),
                "status_channel": channel.status,
                "command": config["command"],
                "workdir": config["workdir"],
                "stop_command": config["stop_command"],
                "health_path": config["health_path"],
                "auto_start": config["auto_start"],
                "auto_restart": config["auto_restart"],
                "stop_on_shutdown": config["stop_on_shutdown"],
                "check_interval_seconds": config["check_interval_seconds"],
                "port": port_of_url(channel.base_url),
                **{k: v for k, v in state.items() if k not in ("channel_id", "channel_name")},
            }
        )
    return {
        "items": items,
        "enabled": ctx.settings.get_bool("services.supervisor_enabled", True),
        "interval_seconds": ctx.settings.get_int("services.check_interval_seconds", 15),
        "autostart_on_boot": ctx.settings.get_bool("services.autostart_on_boot", True),
        "stop_on_shutdown": ctx.settings.get_bool("services.stop_on_shutdown", False),
        "log_dir": str(ctx.services.log_dir) if ctx.services else "",
    }


@router.post("/services/bulk/{action}")
async def bulk_services(action: str, request: Request):
    if action not in {"start", "stop", "restart", "check"}:
        raise bad_request("action 只能是 start / stop / restart / check")
    ctx = request.app.state.ctx
    if ctx.services is None:
        raise bad_request("本地服务托管尚未初始化")
    return {"ok": True, "action": action, "items": await ctx.services.bulk(action)}


@router.post("/services/{channel_id}/{action}")
async def control_service(
    channel_id: str, action: str, request: Request, session: Any = Depends(db_session)
):
    """启动 / 停止 / 重启 / 立即探活 某个渠道的本地服务。"""
    if action not in {"start", "stop", "restart", "check"}:
        raise bad_request("action 只能是 start / stop / restart / check")
    ctx = request.app.state.ctx
    if ctx.services is None:
        raise bad_request("本地服务托管尚未初始化")
    channel = await _channel_for(session, ctx, channel_id)
    config = channel.lifecycle_config()
    if not config["enabled"]:
        raise bad_request(f"渠道「{channel.name}」没有启用本地服务托管", param="lifecycle")
    if action == "start":
        state = await ctx.services.start(channel)
    elif action == "stop":
        state = await ctx.services.stop(channel)
    elif action == "restart":
        state = await ctx.services.restart(channel)
    else:
        state = await ctx.services.check_now(channel)
    return state.to_dict()


@router.get("/services/{channel_id}/log")
async def service_log(
    channel_id: str,
    request: Request,
    session: Any = Depends(db_session),
    lines: int = Query(200, ge=1, le=5000),
):
    """查看托管进程的输出（stdout/stderr 会落到数据目录 logs/services/）。"""
    ctx = request.app.state.ctx
    if ctx.services is None:
        raise bad_request("本地服务托管尚未初始化")
    await _channel_for(session, ctx, channel_id)
    return ctx.services.tail_log(channel_id, lines=lines)


@router.get("/health")
async def health(request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    channels = await ctx.channels.enabled_channels(session)
    health_map = ctx.router.health_snapshot()
    return {
        "ready": ctx.ready,
        "uptime_seconds": round(ctx.uptime_seconds(), 1),
        "channels": [
            {
                "channel_id": channel.channel_id,
                "name": channel.name,
                "provider_type": channel.provider_type,
                "status": channel.status,
                "last_ok_at": channel.last_ok_at.isoformat() + "Z" if channel.last_ok_at else None,
                "last_error": channel.last_error,
                "consecutive_failures": channel.consecutive_failures,
                **health_map.get(channel.channel_id, {}),
            }
            for channel in channels
        ],
        "ratelimit": ctx.ratelimiter.usage_snapshot(),
        "live": ctx.live.snapshot(
            stale_seconds=ctx.settings.get_int("monitoring.stale_seconds", 300)
        )["stats"],
        "sqlite_version": ctx.sqlite_version,
    }


# =========================================================================== #
# 余额
# =========================================================================== #
@router.get("/balance")
async def balance_list(request: Request, session: Any = Depends(db_session)):
    ctx = request.app.state.ctx
    return {
        "items": await ctx.balance.overview(session),
        "warn_threshold": ctx.settings.get_float("balance.warn_threshold", 0.0),
    }


@router.post("/balance/refresh")
async def balance_refresh(
    payload: schemas.BalanceRefresh, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    if ctx.http is None:
        raise bad_request("网关尚未完成初始化")
    items = await ctx.balance.refresh(session, ctx.http, channel_id=payload.channel_id)
    return {"ok": True, "items": items}


@router.get("/balance/{channel_id}/history")
async def balance_history(
    channel_id: str,
    request: Request,
    session: Any = Depends(db_session),
    limit: int = Query(100, ge=1, le=1000),
):
    ctx = request.app.state.ctx
    return {"items": await ctx.balance.history(session, channel_id, limit=limit)}


# =========================================================================== #
# 设置
# =========================================================================== #
@router.get("/settings")
async def get_settings(request: Request):
    ctx = request.app.state.ctx
    schema = ctx.settings.describe()
    values = {key: ctx.settings.get(key) for item in schema for key in [i["key"] for i in item["items"]]}
    values["pricing.models"] = ctx.settings.get_json("pricing.models", {})
    values["pricing.default"] = ctx.settings.get_json("pricing.default", {})
    return {
        "values": values,
        "schema": schema,
        "pending_restart": ctx.pending_restart,
        "restart_managed": ctx.restart_callback is not None,
        "data_dir": str(ctx.paths.data_dir),
    }


@router.put("/settings")
async def update_settings(
    payload: schemas.SettingsUpdate, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    if not payload.values:
        raise bad_request("没有需要更新的设置项")
    changes = await ctx.settings.update(session, payload.values)
    return {
        "ok": True,
        "changed": [
            {
                "key": change.key,
                "old": change.old,
                "new": change.new,
                "requires_restart": change.requires_restart,
            }
            for change in changes
        ],
        "pending_restart": ctx.pending_restart,
    }


@router.post("/settings/reset")
async def reset_settings(
    payload: schemas.SettingsReset, request: Request, session: Any = Depends(db_session)
):
    ctx = request.app.state.ctx
    changes = await ctx.settings.reset(session, payload.keys)
    return {"ok": True, "changed": [change.key for change in changes]}
