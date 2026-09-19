"""控制台静态资源：把 WebView / 浏览器指向 `/admin` 即可。

静态文件不做构建，纯原生 JS + CSS，离线可用（NAS 常常没有外网）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

CONSOLE_PATH = "/admin"
NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}
ASSET_PLACEHOLDER = "{{ASSET_V}}"


class NoCacheStaticFiles(StaticFiles):
    """控制台静态资源每次都回源校验。

    `/admin` 返回的 HTML 已经是 no-store，但 app.js / style.css 走 StaticFiles
    默认不带任何缓存指令，浏览器会按「启发式新鲜度」自己缓存上几小时。网关升级或
    重启之后，控制台窗口可能还在跑旧的 app.js——表现出来就是「界面加载不出来」。
    加上 no-cache（仍可缓存，但每次带 ETag 回源校验，没变就是 304）即可根治。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _asset_stamp(web_dir: Path) -> str:
    """静态资源版本号：取前端文件的修改时间。

    HTML 每次都重新取，于是资源 URL 里的版本号一变，浏览器必然重新下载，
    不会出现「新后端配旧前端」这种最难查的组合。
    """
    newest = 0.0
    for name in ("app.js", "style.css"):
        path = web_dir / name
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return str(int(newest))


def _index_path(web_dir: Path | None) -> Path | None:
    if web_dir is None:
        return None
    candidate = web_dir / "index.html"
    return candidate if candidate.is_file() else None


def mount_console(app, web_dir: Path | None) -> None:
    """挂载静态目录；找不到前端文件时给出可读的提示页。

    路由每次新建：模块级路由是共享对象，被挂两次就会留下两条同路径的处理器，
    先注册的那条永远优先——测试里同时存在多个 app 时就会出现「清理逻辑不生效」
    这类莫名其妙的顺序依赖。
    """
    router = APIRouter(include_in_schema=False)
    if web_dir is not None and web_dir.is_dir():
        app.mount("/static", NoCacheStaticFiles(directory=str(web_dir)), name="static")

    @router.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url=CONSOLE_PATH)

    @router.get("/admin")
    async def console(request: Request):
        index = _index_path(web_dir)
        if index is None:
            return HTMLResponse(_missing_page(), status_code=503)
        html = index.read_text(encoding="utf-8").replace(ASSET_PLACEHOLDER, _asset_stamp(web_dir))
        return HTMLResponse(html, headers=NO_CACHE)

    @router.get("/favicon.ico")
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
