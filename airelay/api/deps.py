"""FastAPI 依赖：上下文获取、协议面鉴权、管理面会话校验。"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import ErrorCode, RelayError, unauthorized
from ..models import ApiKey
from ..security import AdminSession, mask_key

log = logging.getLogger(__name__)

ADMIN_COOKIE = "airelay_session"
ADMIN_HEADER = "X-Admin-Token"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def get_ctx(request: Request) -> Any:
    context = getattr(request.app.state, "ctx", None)
    if context is None:  # pragma: no cover - 装配错误
        raise RelayError(ErrorCode.INTERNAL_ERROR, "应用上下文尚未初始化", status=503)
    return context


def client_ip_of(request: Request, *, trust_proxy: bool = False) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else ""


def is_loopback(host: str) -> bool:
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    return host.startswith("127.") or host == "::1"


async def db_session(request: Request) -> AsyncSession:
    """请求级数据库会话。"""
    ctx = get_ctx(request)
    async with ctx.session_factory() as session:
        yield session


# --------------------------------------------------------------------------- #
# 协议面鉴权
# --------------------------------------------------------------------------- #
def extract_bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # 少数客户端用 x-api-key（Anthropic 风格）直连本地网关
    alt = request.headers.get("x-api-key")
    if alt:
        return alt.strip()
    query = request.query_params.get("api_key")
    return query.strip() if query else ""


async def require_api_key(request: Request) -> ApiKey:
    ctx = get_ctx(request)
    raw = extract_bearer(request)
    async with ctx.session_factory() as session:
        key = await ctx.keys.authenticate(session, raw)
        # 会话已关闭，返回的是脱离会话的实例；后续只读取标量字段
        session.expunge(key)
    return key


# --------------------------------------------------------------------------- #
# 管理面会话
# --------------------------------------------------------------------------- #
async def require_admin(request: Request) -> AdminSession:
    ctx = get_ctx(request)
    token = request.headers.get(ADMIN_HEADER) or request.query_params.get("admin_token") or ""
    if token and hmac.compare_digest(token, ctx.admin_token):
        session = AdminSession(subject="token", client="header")
        session.issued_at = 0
        session.expires_at = 2**31
        return session

    cookie = request.cookies.get(ADMIN_COOKIE) or ""
    if cookie:
        session = ctx.signer.verify(cookie)
        if session is not None:
            return session

    if _remote_allowed_without_token(request, ctx):
        session = AdminSession(subject="loopback", client="loopback")
        session.issued_at = 0
        session.expires_at = 2**31
        return session

    raise unauthorized("管理面需要登录：请提供管理员令牌")


def _remote_allowed_without_token(request: Request, ctx: Any) -> bool:
    """环回来源直接放行；远程来源是否免令牌由设置决定（默认必须带令牌）。"""
    host = client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False))
    if is_loopback(host):
        return True
    return not ctx.settings.get_bool("security.require_admin_token_remote", True)


def issue_session_cookie(request: Request, ctx: Any, *, client: str) -> tuple[str, int]:
    token = ctx.signer.issue(client=client)
    return token, int(ctx.signer.ttl_seconds)


def can_auto_login(request: Request, ctx: Any) -> bool:
    return _remote_allowed_without_token(request, ctx)


__all__ = [
    "ADMIN_COOKIE",
    "ADMIN_HEADER",
    "can_auto_login",
    "client_ip_of",
    "db_session",
    "extract_bearer",
    "get_ctx",
    "is_loopback",
    "issue_session_cookie",
    "mask_key",
    "require_admin",
    "require_api_key",
]
