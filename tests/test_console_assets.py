"""控制台静态资源的交付方式：缓存策略与资源版本号。

这两件事看着像小事，实际决定「改完前端之后控制台能不能立刻用上新代码」：
HTML 若被缓存，用户看到的永远是旧页面；app.js 若带启发式新鲜度，浏览器会
几小时不回源，于是出现「后端已升级、前端还是旧的」这种最难查的组合。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from airelay.api import ui

pytestmark = pytest.mark.anyio

WEB_DIR = Path(__file__).resolve().parents[1] / "airelay" / "web"


def standalone_app(web_dir) -> FastAPI:
    app = FastAPI()
    ui.mount_console(app, web_dir)
    return app


async def get(app: FastAPI, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay.test") as http:
        return await http.get(path, **kwargs)


async def test_console_html_is_not_cached_and_busts_asset_urls(client) -> None:
    response = await client.get("/admin")
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")
    html = response.text
    assert "{{ASSET_V}}" not in html, "占位符必须被替换成真实版本号"
    assert "/static/app.js?v=" in html
    assert "/static/style.css?v=" in html


async def test_console_html_missing_web_dir_returns_hint_page() -> None:
    """前端文件缺失时给可读提示，而不是 500 或白屏。"""
    response = await get(standalone_app(None), "/admin")
    assert response.status_code == 503
    assert "text/html" in response.headers["content-type"]
    assert "控制台静态文件" in response.text


async def test_root_redirects_to_console(client) -> None:
    response = await client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == ui.CONSOLE_PATH


async def test_asset_version_follows_file_mtime(tmp_path) -> None:
    """版本号取前端文件 mtime：改了文件，URL 就变，浏览器必然重新下载。"""
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text(
        '<link href="/static/style.css?v={{ASSET_V}}"><script src="/static/app.js?v={{ASSET_V}}"></script>',
        encoding="utf-8",
    )
    (web_dir / "app.js").write_text("// v1", encoding="utf-8")
    (web_dir / "style.css").write_text("body{}", encoding="utf-8")

    app = standalone_app(web_dir)
    first = (await get(app, "/admin")).text
    stamp = str(int((web_dir / "app.js").stat().st_mtime))
    assert f"app.js?v={stamp}" in first

    (web_dir / "app.js").write_text("// v2 改过", encoding="utf-8")
    import os

    os.utime(web_dir / "app.js", (0, (web_dir / "app.js").stat().st_mtime + 5))
    second = (await get(standalone_app(web_dir), "/admin")).text
    assert second != first, "文件变更后版本号必须跟着变"


async def test_static_assets_revalidate_every_time(client) -> None:
    response = await client.get("/static/app.js")
    assert response.status_code == 200
    # no-cache 表示「可以存，但每次都要回源校验」；未变更时服务端回 304
    assert response.headers.get("cache-control") == "no-cache"
    etag = response.headers.get("etag")
    assert etag

    again = await client.get("/static/app.js", headers={"if-none-match": etag})
    assert again.status_code == 304
    assert again.headers.get("cache-control") == "no-cache"


def test_toggle_state_is_read_through_helper() -> None:
    """开关状态一律用 `toggleValue(节点)` 读，不要现场 `节点.querySelector('input').checked`。

    踩过的坑：把开关的 `<input>` 挪进新容器后，原节点里就查不到 input 了，读 `.checked`
    抛 TypeError；异常只落在浏览器控制台里，界面上表现为「点创建没反应」——请求根本没发出去。
    这条断言按约定挡住同类写法（`toggleValue` 内部已经做了空值兜底）。
    """
    source = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "querySelector('input').checked" not in source


def test_model_picker_keeps_panel_out_of_the_chip_row() -> None:
    """模型候选面板与标签行必须分属两层节点。

    踩过的坑：面板原本挂在标签行容器里，而标签行每次加标签都用 `replaceChildren` 重绘，
    于是「加一个标签，下拉面板就跟着消失」——看着像下拉坏了，其实是被重绘连带清掉了。
    现在外层是 `pickwrap`（标签行 + 面板），标签行只重绘自己那一层。
    """
    source = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "class: 'pickwrap'" in source
    assert "root.append(wrapper, panel)" in source


def test_model_picker_keyboard_nav_uses_names_not_nodes() -> None:
    """键盘导航记的是候选**名字**的下标，不是 DOM 节点。

    踩过的坑：早先拿节点当高亮值，回车时把节点拼进标签里，白名单里就出现
    `[object HTMLButtonElement]`——看着像数据脏了，其实是前端把节点当字符串用了。
    """
    source = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "let flat = [];" in source and "let hot = -1;" in source
    assert "addTag(flat[hot])" in source


def test_channel_health_verdict_ignores_stale_errors() -> None:
    """健康徽记不能只看 `last_error` 非空——那个字段成功后故意不擦。

    踩过的坑：上游一时忙、或用户发了一张上游不认的图片，`last_error` 就永久留着，
    于是早已恢复的渠道一直挂红「失败」。判定改走 `channelFailing()`：比较错误与
    最近一次成功的时间先后。
    """
    source = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "channelFailing(channel)" in source
    assert ": (channel.last_error\n" not in source
