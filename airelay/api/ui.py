"""控制台静态资源：把 WebView / 浏览器指向 `/admin` 即可。

静态文件不做构建，纯原生 JS + CSS，离线可用（NAS 常常没有外网）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

router = APIRouter(tags=["控制台"])

CONSOLE_PATH = "/admin"
NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


def _index_path(web_dir: Path | None) -> Path | None:
    if web_dir is None:
        return None
    candidate = web_dir / "index.html"
    return candidate if candidate.is_file() else None


def mount_console(app, web_dir: Path | None) -> None:
    """挂载静态目录；找不到前端文件时给出可读的提示页。"""
    if web_dir is not None and web_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @router.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url=CONSOLE_PATH)

    @router.get("/admin", include_in_schema=False)
    async def console(request: Request):
        index = _index_path(web_dir)
        if index is None:
            return HTMLResponse(_missing_page(), status_code=503)
        return FileResponse(index, media_type="text/html; charset=utf-8", headers=NO_CACHE)

    @router.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        if web_dir is not None:
            icon = web_dir / "favicon.svg"
            if icon.is_file():
                return FileResponse(icon, media_type="image/svg+xml")
        return HTMLResponse(status_code=204)

    app.include_router(router)


def _missing_page() -> str:
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>控制台资源缺失</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0d1117;color:#e6edf3;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{max-width:620px;padding:32px;border:1px solid #30363d;border-radius:14px;background:#161b22}
code{background:#0d1117;padding:2px 6px;border-radius:6px;color:#79c0ff}
h1{font-size:20px;margin:0 0 12px}
p{line-height:1.7;color:#9198a1}
</style></head><body><div class="card">
<h1>未找到控制台静态文件</h1>
<p>进程已经跑起来了，但没有在预期位置找到 <code>web/index.html</code>。</p>
<p>若你是从源码运行，请确认 <code>airelay/web</code> 目录完整（打包分发的单目录模式也需要带上它）。</p>
<p>协议面不受影响，仍可直接用 <code>curl</code> 或任意 OpenAI 客户端访问 <code>/v1/chat/completions</code>。</p>
<p>管理接口同样可用：在 <code>/docs</code> 里能看到全部端点。</p>
</div></body></html>"""
