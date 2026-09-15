"""开发/演示数据播种：起好网关后跑一次，得到渠道、别名、密钥与一批用量。

    python scripts/seed_demo.py --base http://127.0.0.1:8137 --token <管理员令牌>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def call(base: str, token: str, method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"X-Admin-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"message": raw}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--token", required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:9001")
    parser.add_argument("--chat", type=int, default=0, help="额外发起多少次聊天请求以产生用量")
    args = parser.parse_args()

    base, token = args.base.rstrip("/"), args.token

    # 清理同名演示数据，避免重复播种堆积
    for path in ("/api/admin/channels", "/api/admin/keys"):
        _, listing = call(base, token, "GET", path)
        for item in (listing or {}).get("items", []):
            if item.get("name", "").startswith(("DeepSeek", "Claude", "给 ", "演示")):
                call(base, token, "DELETE", f"{path}/{item.get('channel_id') or item.get('key_id')}")

    channels = [
        {
            "name": "DeepSeek 主力",
            "provider_type": "deepseek",
            "base_url": args.upstream,
            "api_key": "sk-upstream-demo-key-1234",
            "priority": 0,
            "weight": 3,
            "models": ["fast", "deepseek-*"],
            "note": "日常主力通道",
        },
        {
            "name": "Claude 备用",
            "provider_type": "anthropic",
            "base_url": args.upstream,
            "api_key": "sk-ant-demo-5678",
            "priority": 1,
            "weight": 1,
            "models": ["claude-*"],
            "note": "长文场景备用",
        },
    ]
    for channel in channels:
        status, body = call(base, token, "POST", "/api/admin/channels", channel)
        print(f"渠道 {channel['name']}: {status} {body.get('channel_id') or body.get('message')}")

    maps = [
        {"alias": "fast", "upstream_model": "mock-gpt-small", "note": "日常快模型"},
        {"alias": "claude-*", "upstream_model": "mock-gpt-large", "note": "通配示例"},
    ]
    for entry in maps:
        status, body = call(base, token, "POST", "/api/admin/models/map", entry)
        if status >= 400:
            print(f"别名 {entry['alias']}: {status} {body.get('message')}")
    print(f"别名：{len(maps)} 条已就绪")

    status, body = call(
        base,
        token,
        "POST",
        "/api/admin/keys",
        {
            "name": "给 Cursor 用",
            "quota_limit": 5_000_000,
            "rpm_limit": 60,
            "tpm_limit": 200_000,
            "model_allowed": ["fast", "claude-*"],
            "note": "本机编辑器",
            "expires_in_days": 90,
        },
    )
    if status >= 400:
        print("创建密钥失败：", body)
        return 1
    plaintext = body["plaintext"]
    print("本地密钥（明文只此一次）：", plaintext)
    with open("devdata_key.txt", "w", encoding="utf-8") as handle:
        handle.write(plaintext)

    # 产生一些用量，让图表有东西可看
    for index in range(args.chat):
        payload = json.dumps(
            {
                "model": "claude-4" if index % 4 == 3 else "fast",
                "messages": [{"role": "user", "content": f"第 {index + 1} 次演示请求"}],
                "stream": index % 2 == 1,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {plaintext}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
            time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            print("请求失败：", exc)
    if args.chat:
        print(f"已产生 {args.chat} 次用量")
        # 额外制造一条失败记录，便于查看错误态样式
        payload = json.dumps({"model": "no-such-model", "messages": [{"role": "user", "content": "x"}]}).encode()
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {plaintext}", "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=15).read()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
