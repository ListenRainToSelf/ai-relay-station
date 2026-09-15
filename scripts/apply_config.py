"""按 JSON 规格批量接入渠道 / 别名 / 密钥，并做连通性与模型自检。

用途：
  1) 新机器上一次把渠道配好（不用在界面上一条条点）；
  2) 数据目录被清空后快速恢复；
  3) 换供应商时批量试跑。

规格文件示例（**含密钥，注意别提交到仓库**）：

    {
      "channels": [
        {"name": "DeepSeek", "provider_type": "deepseek",
         "base_url": "https://api.deepseek.com", "api_key": "sk-...",
         "models": ["deepseek-*"], "priority": 0, "weight": 1},
        {"name": "小米 MiMo", "provider_type": "openai",
         "base_url": "https://api.xiaomimimo.com/v1", "api_key": "sk-...",
         "models": ["mimo-*"], "priority": 1, "weight": 1}
      ],
      "model_map": [{"alias": "fast", "upstream_model": "deepseek-chat"}],
      "keys": [{"name": "默认密钥", "quota_limit": 0, "model_allowed": []}]
    }

用法：

    python scripts/apply_config.py --spec spec.json --token <管理员令牌>
    python scripts/apply_config.py --spec spec.json --url http://127.0.0.1:8412 \\
        --token <令牌> --probe --list-models --key-out new_keys.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = os.environ.get("AIRELAY_URL", "http://127.0.0.1:8000")


class Api:
    def __init__(self, base: str, token: str, *, timeout: float = 120.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body: Any = None, *, timeout: float | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"X-Admin-Token": self.token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw) if raw else None
            except ValueError:
                return error.code, {"message": raw.decode("utf-8", "replace")}

    # 便捷方法
    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)

    def put(self, path: str, body: Any = None, **kw):
        return self.call("PUT", path, body if body is not None else {}, **kw)


def ok(mark: bool) -> str:
    return "✓" if mark else "✗"


def apply_spec(
    api: Api,
    spec: dict[str, Any],
    *,
    probe: bool,
    list_models: bool,
    key_out: Path | None,
) -> int:
    failures = 0

    # ---------------------------------------------------------------- 渠道
    _, existing = api.get("/api/admin/channels")
    by_name = {item["name"]: item for item in (existing or {}).get("items", [])}
    channel_ids: dict[str, str] = {}

    for channel in spec.get("channels", []):
        name = channel.get("name") or channel.get("provider_type")
        payload = dict(channel)
        current = by_name.get(name)
        if current:
            channel_id = current["channel_id"]
            payload["api_key"] = payload.get("api_key") or None
            status, body = api.put(f"/api/admin/channels/{channel_id}", payload)
            print(f"  {ok(status < 400)} 更新渠道 {name} → {status} {body.get('message', '') if status >= 400 else ''}")
        else:
            status, body = api.post("/api/admin/channels", payload)
            channel_id = (body or {}).get("channel_id", "")
            print(f"  {ok(status < 400)} 新建渠道 {name} → {status} {body.get('message', '') if status >= 400 else channel_id}")
        if status >= 400:
            failures += 1
            continue
        channel_ids[name] = channel_id

        if list_models:
            status, body = api.get(f"/api/admin/channels/{channel_id}/models", timeout=60)
            if status < 400:
                ids = [item["id"] for item in (body or {}).get("items", [])]
                print(f"      上游模型（{len(ids)} 个）：{', '.join(ids[:20])}{' …' if len(ids) > 20 else ''}")
            else:
                print(f"      模型列表不可用（{status}）{(body or {}).get('message', '')}")

        if probe:
            model = (channel.get("probe_model") or
                     next((m for m in channel.get("models", []) if "*" not in m), ""))
            status, body = api.post(
                f"/api/admin/channels/{channel_id}/probe", {"model": model}, timeout=120
            )
            body = body or {}
            if body.get("ok"):
                print(f"      探针 ✓ {body.get('latency_ms')}ms · 模型 {body.get('model')} · "
                      f"回复「{(body.get('reply') or '')[:40]}」· usage {body.get('usage')}")
            else:
                failures += 1
                print(f"      探针 ✗ {body.get('code', status)} {body.get('message', '')}")

    # ---------------------------------------------------------------- 别名
    _, maps = api.get("/api/admin/models/map")
    alias_index = {item["alias"]: item for item in (maps or {}).get("items", [])}
    for entry in spec.get("model_map", []):
        alias = entry["alias"]
        payload = dict(entry)
        if entry.get("channel"):
            payload["channel_id"] = channel_ids.get(entry["channel"])
            payload.pop("channel", None)
        if alias in alias_index:
            status, body = api.put(f"/api/admin/models/map/{alias_index[alias]['id']}", payload)
            action = "更新"
        else:
            status, body = api.post("/api/admin/models/map", payload)
            action = "新建"
        print(f"  {ok(status < 400)} {action}别名 {alias} → {entry['upstream_model']}"
              f"{'' if status < 400 else '  ' + str((body or {}).get('message'))}")
        if status >= 400:
            failures += 1

    # ---------------------------------------------------------------- 密钥
    created_keys: list[dict[str, Any]] = []
    for key in spec.get("keys", []):
        status, body = api.post("/api/admin/keys", key)
        if status < 400:
            created_keys.append({"name": key.get("name"), "plaintext": body["plaintext"],
                                 "key_id": body["key"]["key_id"]})
            print(f"  ✓ 新建密钥 {key.get('name')} → {body['plaintext'][:22]}…（明文只在创建时出现）")
        else:
            failures += 1
            print(f"  ✗ 新建密钥 {key.get('name')} → {status} {(body or {}).get('message')}")

    if created_keys and key_out:
        key_out.parent.mkdir(parents=True, exist_ok=True)
        key_out.write_text(
            "\n".join(f"{item['name']}\t{item['plaintext']}" for item in created_keys),
            encoding="utf-8",
        )
        print(f"\n  新密钥明文已写入：{key_out}（请妥善保存后删除）")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="按 JSON 规格批量配置中转站")
    parser.add_argument("--spec", required=True, help="规格 JSON 路径")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default=os.environ.get("AIRELAY_ADMIN_TOKEN", ""))
    parser.add_argument("--probe", action="store_true", help="每个渠道做一次真实调用探针")
    parser.add_argument("--list-models", action="store_true", help="打印上游模型列表")
    parser.add_argument("--key-out", default="", help="把新建密钥的明文写到该文件")
    args = parser.parse_args()

    if not args.token:
        print("缺少管理员令牌：用 --token 或环境变量 AIRELAY_ADMIN_TOKEN 传入", file=sys.stderr)
        return 2

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    api = Api(args.url, args.token)

    status, health = api.get("/healthz", timeout=10)
    print(f"网关 {args.url} → HTTP {status} {health}")
    if status >= 400:
        return 1

    started = time.time()
    print("\n[渠道]")
    failures = apply_spec(
        api,
        spec,
        probe=args.probe,
        list_models=args.list_models,
        key_out=Path(args.key_out).expanduser() if args.key_out else None,
    )
    print(f"\n耗时 {time.time() - started:.1f}s，失败项 {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
