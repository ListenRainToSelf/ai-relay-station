"""FastAPI 应用工厂：装配三个面（协议面 / 管理面 / 应用面）。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, gateway, ui, ws
from .context import AppContext
from .errors import ErrorCode, RelayError
from .version import APP_NAME, __version__

log = logging.getLogger(__name__)


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.startup()
        try:
            yield
        finally:
            await ctx.shutdown()

    app = FastAPI(
        title=f"{APP_NAME} 网关",
        description=(
            "本机/局域网自建的 AI API 聚合中转网关。\n\n"
            "- **协议面** `/v1/*`：OpenAI 兼容，用本地分发密钥鉴权\n"
            "- **管理面** `/api/admin/*`：控制台使用的读写接口，环回地址可直接访问，"
            "远程需管理员令牌\n"
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.ctx = ctx

    origins = _cors_origins(ctx)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id", "X-Airelay-Channel", "X-Airelay-Upstream-Model"],
        )

    _install_error_handlers(app)

    app.include_router(gateway.router)
    app.include_router(admin.session_router)
    app.include_router(admin.router)
    app.include_router(ws.router)
    ui.mount_console(app, ctx.paths.web_dir)

    @app.get("/healthz", tags=["运维"], summary="存活探针")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["运维"], summary="就绪探针")
    async def readyz() -> JSONResponse:
        ok = ctx.ready
        body = {
            "ready": ok,
            "version": __version__,
            "mode": ctx.mode,
            "uptime_seconds": round(ctx.uptime_seconds(), 1),
            "data_dir": str(ctx.paths.data_dir),
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/api/admin/_ping", tags=["运维"], summary="管理面连通性")
    async def admin_ping() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    return app


def _cors_origins(ctx: AppContext) -> list[str]:
    raw = ctx.settings.get_str("network.cors_allow_origins", "")
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RelayError)
    async def relay_error_handler(request: Request, exc: RelayError) -> JSONResponse:
        is_admin = request.url.path.startswith("/api/")
        body = exc.to_admin_body() if is_admin else exc.to_openai_body()
        headers: dict[str, str] = {}
        if exc.request_id:
            # 出错请求也会写用量明细，带上 request_id 就能和「请求明细」对上
            headers["X-Request-Id"] = exc.request_id
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                body["error"]["request_id"] = exc.request_id
        if exc.code == ErrorCode.RATE_LIMITED:
            retry_after = 0
            if isinstance(exc.details, dict):
                retry_after = int(exc.details.get("retry_after") or 0)
            headers["Retry-After"] = str(max(1, retry_after))
        if exc.status >= 500:
            log.warning("请求失败 %s %s → %s %s", request.method, request.url.path, exc.status, exc.code)
        return JSONResponse(body, status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = (exc.errors() or [{}])[0]
        location = ".".join(str(part) for part in first.get("loc", ())[1:]) or None
        message = first.get("msg") or "请求参数校验失败"
        is_admin = request.url.path.startswith("/api/")
        if is_admin:
            return JSONResponse(
                {"ok": False, "code": ErrorCode.BAD_REQUEST, "message": message, "param": location},
                status_code=400,
            )
        return JSONResponse(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "code": ErrorCode.BAD_REQUEST,
                    "param": location,
                }
            },
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        message = "网关内部错误，请查看数据目录下的日志"
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "code": ErrorCode.INTERNAL_ERROR, "message": f"{message}：{exc}"},
                status_code=500,
            )
        return JSONResponse(
            {
                "error": {
                    "message": message,
                    "type": "internal_error",
                    "code": ErrorCode.INTERNAL_ERROR,
                    "param": None,
                }
            },
            status_code=500,
        )
