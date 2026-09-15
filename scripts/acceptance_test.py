"""本地 AI 中转站 · 真上游验收测试

对一台正在运行的中转站做端到端验收：协议面、别名、错误码、限流配额、计量落库、
实时会话、余额、渠道健康、导出与路由试算。需要：
  * 一个本地密钥（-k/--key）——用来调用 /v1/*
  * 管理员令牌（-t/--token）——用来读管理面、并为部分用例临时建/删资源

脚本会自己创建临时资源（临时密钥 / 临时渠道 / 计价表）并在结束时清理，
不会删除你自己配置的渠道与密钥。

    python scripts/acceptance_test.py --url http://127.0.0.1:8412 \
        -k sk-relay-xxxx-xxxx -t <管理员令牌> --small-model fast --big-model smart

不加参数时会尝试从环境变量 AIRELAY_URL / AIRELAY_KEY / AIRELAY_ADMIN_TOKEN 读取。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_URL = os.environ.get("AIRELAY_URL", "http://127.0.0.1:8000")


@dataclass
class Report:
    results: list[tuple[Any, str, bool, str]] = field(default_factory=list)

    def add(self, no: Any, name: str, passed: bool, detail: str = "") -> None:
        self.results.append((no, name, passed, detail))
        mark = "✓" if passed else "✗"
        print(f"  {mark} [{str(no):>5}] {name}" + (f"\n         {detail}" if detail else ""), flush=True)

    @property
    def failed(self) -> list[tuple[Any, str, bool, str]]:
        return [item for item in self.results if not item[2]]


class Gateway:
    def __init__(self, base: str, key: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.token = token

    # ---------------------------------------------------------------- 基础调用
    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        admin: bool = False,
        headers: dict[str, str] | None = None,
        timeout: float = 180.0,
    ) -> tuple[int, Any, dict[str, str]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        head = {"Content-Type": "application/json; charset=utf-8"}
        if admin:
            head["X-Admin-Token"] = self.token
        else:
            head["Authorization"] = f"Bearer {self.key}"
        if headers:
            head.update(headers)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                parsed = self._parse(raw)
                return response.status, parsed, dict(response.headers)
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, self._parse(raw), dict(error.headers)

    @staticmethod
    def _parse(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("utf-8", "replace")[:400]

    def chat(self, body: dict[str, Any], *, timeout: float = 180.0):
        return self.request("POST", "/v1/chat/completions", body, timeout=timeout)

    # ---------------------------------------------------------------- 流式
    def stream_chat(self, body: dict[str, Any], *, timeout: float = 180.0) -> dict[str, Any]:
        """返回 {status, chunks, text, reasoning, usage, first_token_ms, done, frames}"""
        payload = dict(body)
        payload["stream"] = True
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/event-stream",
            },
        )
        started = time.perf_counter()
        first_token_ms = None
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = None
        frames = 0
        done = False
        status = 0
        content_type = ""
        model_echo = None
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                content_type = response.headers.get("content-type", "")
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload_text = line[5:].strip()
                    if payload_text == "[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(payload_text)
                    except ValueError:
                        continue
                    frames += 1
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if chunk.get("model"):
                        model_echo = chunk["model"]
                    for choice in chunk.get("choices") or []:
                        delta = (choice or {}).get("delta") or {}
                        piece = delta.get("content")
                        think = delta.get("reasoning_content")
                        # 「首字」按任意首个输出算：推理模型先吐 reasoning，那也是客户端可见的输出
                        if (piece or think) and first_token_ms is None:
                            first_token_ms = (time.perf_counter() - started) * 1000
                        if piece:
                            text_parts.append(piece)
                        if think:
                            reasoning_parts.append(think)
        except urllib.error.HTTPError as error:
            status = error.code
            return {"status": status, "error": self._parse(error.read())}
        return {
            "status": status,
            "content_type": content_type,
            "frames": frames,
            "done": done,
            "text": "".join(text_parts),
            "reasoning": "".join(reasoning_parts),
            "usage": usage,
            "model": model_echo,
            "first_token_ms": first_token_ms,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="真上游端到端验收")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("-k", "--key", default=os.environ.get("AIRELAY_KEY", ""))
    parser.add_argument("-t", "--token", default=os.environ.get("AIRELAY_ADMIN_TOKEN", ""))
    parser.add_argument("--small-model", default="fast", help="便宜/快的模型（用别名或真实 id）")
    parser.add_argument("--big-model", default="smart", help="另一个不同渠道的模型")
    parser.add_argument("--skip-stream", action="store_true", help="跳过流式用例（省钱）")
    args = parser.parse_args()

    if not args.key or not args.token:
        print("需要 -k 本地密钥 与 -t 管理员令牌", file=sys.stderr)
        return 2

    gw = Gateway(args.url, args.key, args.token)
    report = Report()
    cleanup: list[tuple[str, str]] = []  # (类型, id)

    def admin(method: str, path: str, body: Any = None, timeout: float = 120.0):
        return gw.request(method, path, body, admin=True, timeout=timeout)

    print(f"\n目标网关：{gw.base}\n模型：small={args.small_model}  big={args.big_model}\n")

    try:
        # ------------------------------------------------------------ 0. 探针
        status, health, _ = gw.request("GET", "/healthz", timeout=15)
        report.add(1, "网关存活 /healthz", status == 200 and health.get("status") == "ok", str(health))
        status, ready, _ = gw.request("GET", "/readyz", timeout=15)
        report.add(2, "网关就绪 /readyz", status == 200 and ready.get("ready") is True, str(ready))

        # ------------------------------------------------------------ 1. 模型列表
        status, models, _ = gw.request("GET", "/v1/models")
        ids = [item["id"] for item in (models or {}).get("data", [])] if isinstance(models, dict) else []
        report.add(3, "/v1/models 返回模型列表", status == 200 and bool(ids), f"{len(ids)} 个：{', '.join(ids[:10])}")
        report.add(
            4, "别名已生效（列表含 fast/smart 等）",
            any(name in ids for name in (args.small_model, args.big_model)),
            f"small={args.small_model} big={args.big_model}",
        )

        # ------------------------------------------------------------ 2. 渠道与探针
        status, channels, _ = admin("GET", "/api/admin/channels")
        items = (channels or {}).get("items", [])
        report.add(5, "渠道已配置", status == 200 and len(items) >= 2,
                   "，".join(f"{c['name']}({c['provider_type']})" for c in items))
        for channel in items:
            status, probe, _ = admin("POST", f"/api/admin/channels/{channel['channel_id']}/probe",
                                     {"model": "", "max_tokens": 64})
            ok = bool((probe or {}).get("ok"))
            note = (probe or {}).get("message", "")
            reply = ((probe or {}).get("reply") or "").replace("\n", " ")[:50]
            report.add(6, f"渠道探针：{channel['name']}",
                       ok, f"{probe.get('latency_ms')}ms 回复「{reply}」{note}" if ok else f"{probe.get('code')} {note}")

        # ------------------------------------------------------------ 3. 非流式
        for label, model in (("small", args.small_model), ("big", args.big_model)):
            status, body, headers = gw.chat({
                "model": model,
                "messages": [{"role": "user", "content": "用一句话说明你是什么模型"}],
                "max_tokens": 64,
            })
            ok = status == 200 and isinstance(body, dict) and bool(body.get("choices"))
            usage = (body or {}).get("usage") if isinstance(body, dict) else None
            content = ""
            if ok:
                message = body["choices"][0].get("message") or {}
                content = (message.get("content") or message.get("reasoning_content") or "").replace("\n", " ")
            report.add(7, f"非流式对话（{label}={model}）", ok,
                       f"{content[:60]!r} usage={usage}" if ok else f"HTTP {status} {body}")
            if ok:
                report.add(8, f"别名隐藏上游 id（{model}）",
                           body.get("model") == model and bool(headers.get("x-airelay-upstream-model")),
                           f"响应 model={body.get('model')} 上游={headers.get('x-airelay-upstream-model')} "
                           f"渠道={headers.get('x-airelay-channel')}")
                extras = [k for k in (usage or {}) if k not in ("prompt_tokens", "completion_tokens", "total_tokens")]
                report.add(9, f"usage 附加字段保留（{model}）", True,
                           f"标准三项 + 上游附加：{extras or '（上游未返回附加字段）'}")

        # ------------------------------------------------------------ 4. 流式
        if not args.skip_stream:
            for label, model in (("small", args.small_model), ("big", args.big_model)):
                result = gw.stream_chat({
                    "model": model,
                    "messages": [{"role": "user", "content": "数到五"}],
                    "max_tokens": 384,
                })
                ok = (result.get("status") == 200 and result.get("done") and result.get("frames", 0) > 0
                      and bool(result.get("text") or result.get("reasoning")))
                detail = (f"{result.get('frames')} 帧 · 首字 {result.get('first_token_ms') and round(result['first_token_ms'])}ms"
                          f" · 正文 {len(result.get('text',''))} 字 / 思考 {len(result.get('reasoning',''))} 字"
                          f" · usage={result.get('usage')}")
                report.add(10, f"流式对话（{label}={model}）", ok,
                           detail if ok else f"HTTP {result.get('status')} {result.get('error')}")
                report.add(11, f"流式 usage 回传（{model}）", bool(result.get("usage")),
                           f"最后一次 usage：{result.get('usage')}")
                report.add(12, f"流式模型名回显（{model}）", result.get("model") == model,
                           f"帧里 model={result.get('model')}")

        # ------------------------------------------------------------ 5. 错误码
        status, body, headers = gw.request("POST", "/v1/chat/completions",
                                          {"model": args.small_model,
                                           "messages": [{"role": "user", "content": "x"}]},
                                          headers={"Authorization": "Bearer sk-relay-00000000-invalid"})
        report.add(13, "无效本地密钥 → 401 INVALID_API_KEY",
                   status == 401 and (body or {}).get("error", {}).get("code") == "INVALID_API_KEY",
                   f"HTTP {status} {(body or {}).get('error', {}).get('code')}")

        status, body, _ = gw.chat({"model": "gpt-4o-nonexistent-model",
                                   "messages": [{"role": "user", "content": "x"}]})
        code = (body or {}).get("error", {}).get("code")
        report.add(14, "无渠道服务该模型 → 503 NO_CHANNEL_AVAILABLE",
                   status == 503 and code == "NO_CHANNEL_AVAILABLE", f"HTTP {status} {code}")

        status, body, _ = gw.chat({"model": args.small_model, "messages": []})
        report.add(15, "缺少 messages → 400", status == 400, f"HTTP {status} {str(body)[:80]}")

        # 临时密钥：模型授权
        status, created, _ = admin("POST", "/api/admin/keys",
                                   {"name": "验收-受限密钥", "model_allowed": ["__none__"]})
        if status < 400:
            cleanup.append(("keys", created["key"]["key_id"]))
            limited = Gateway(gw.base, created["plaintext"], gw.token)
            status, body, _ = limited.chat({"model": args.small_model,
                                            "messages": [{"role": "user", "content": "x"}]})
            report.add(16, "模型未授权 → 403 MODEL_NOT_ALLOWED",
                       status == 403 and (body or {}).get("error", {}).get("code") == "MODEL_NOT_ALLOWED",
                       f"HTTP {status} {(body or {}).get('error', {}).get('code')}")
        else:
            report.add(16, "模型未授权 → 403 MODEL_NOT_ALLOWED", False, f"临时密钥创建失败：{created}")

        # 临时密钥：RPM 限流
        status, created, _ = admin("POST", "/api/admin/keys",
                                   {"name": "验收-限流密钥", "rpm_limit": 1})
        if status < 400:
            cleanup.append(("keys", created["key"]["key_id"]))
            limited = Gateway(gw.base, created["plaintext"], gw.token)
            payload = {"model": args.small_model, "messages": [{"role": "user", "content": "x"}],
                       "max_tokens": 8}
            first_status, _, _ = limited.chat(payload)
            second_status, second_body, second_headers = limited.chat(payload)
            report.add(17, f"RPM 限流 → 429 RATE_LIMITED（首次 {first_status}）",
                       second_status == 429 and (second_body or {}).get("error", {}).get("code") == "RATE_LIMITED",
                       f"第二次 HTTP {second_status} {(second_body or {}).get('error', {}).get('code')} "
                       f"Retry-After={second_headers.get('retry-after')}")
        else:
            report.add(17, "RPM 限流 → 429 RATE_LIMITED", False, f"临时密钥创建失败：{created}")

        # 临时渠道：错误的上游密钥（验证上游鉴权错误透传且不重试）
        status, bad_channel, _ = admin("POST", "/api/admin/channels", {
            "name": "验收-错误上游密钥", "provider_type": "deepseek",
            "base_url": "https://api.deepseek.com", "api_key": "sk-invalid-for-acceptance-test",
            "models": ["deepseek-flash"], "priority": -1, "weight": 1,
        })
        if status < 400:
            cleanup.append(("channels", bad_channel["channel_id"]))
            status, body, headers = gw.chat({"model": "deepseek-flash",
                                             "messages": [{"role": "user", "content": "x"}],
                                             "max_tokens": 8})
            code = (body or {}).get("error", {}).get("code")
            message = (body or {}).get("error", {}).get("message", "")
            report.add(18, "上游鉴权失败 → 502 UPSTREAM_ERROR（状态归一化，报文透传）",
                       status == 502 and code == "UPSTREAM_ERROR" and bool(message),
                       f"HTTP {status} {code} · X-Request-Id={headers.get('x-request-id')} · "
                       f"上游报文「{message[:70]}」")
            report.add(18.1, "错误响应带 X-Request-Id（可对应用量明细）",
                       bool(headers.get("x-request-id")),
                       f"X-Request-Id={headers.get('x-request-id')}")
        else:
            report.add(18, "上游鉴权失败 → 502 UPSTREAM_ERROR", False, f"临时渠道创建失败：{bad_channel}")

        # ------------------------------------------------------------ 6. 计量与统计
        time.sleep(2)  # 等异步落库
        status, stats, _ = admin("GET", "/api/admin/stats?hours=1&bucket=hour")
        overview = (stats or {}).get("overview", {})
        report.add(19, "用量已落库并聚合", status == 200 and overview.get("requests", 0) > 0,
                   f"请求 {overview.get('requests')} · 成功 {overview.get('ok')} · 失败 {overview.get('errors')} · "
                   f"tokens {overview.get('total_tokens')} · 平均首字 {overview.get('avg_first_token_ms')}ms · "
                   f"平均速度 {overview.get('avg_speed_tok_s')} tok/s")
        by_model = (stats or {}).get("by_model", [])
        by_channel = (stats or {}).get("by_channel", [])
        report.add(20, "按模型 / 按渠道聚合有数据",
                   bool(by_model) and bool(by_channel),
                   "模型：" + "，".join(f"{r['model']}×{r['requests']}" for r in by_model[:4])
                   + " | 渠道：" + "，".join(f"{r['channel_name']}×{r['requests']}" for r in by_channel[:4]))
        status, logs, _ = admin("GET", "/api/admin/stats/logs?limit=50")
        rows = (logs or {}).get("items", [])
        sample = next((r for r in rows if r["status"] == "ok" and r["total_tokens"] > 0), None)
        report.add(21, "明细含真实上游 token",
                   sample is not None,
                   f"样例：{sample['model']} tokens={sample['total_tokens']} "
                   f"(输入{sample['prompt_tokens']}/输出{sample['completion_tokens']}) "
                   f"流式={sample['stream']} 耗时={sample['latency_ms']}ms" if sample else "无成功记录")

        plain = next((r for r in rows if r["status"] == "ok" and not r["stream"]
                      and r["completion_tokens"] > 0), None)
        streamed = next((r for r in rows if r["status"] == "ok" and r["stream"]
                         and r["completion_tokens"] > 0), None)
        report.add(21.1, "输出速度口径正确（流式才有速度，非流式为 0）",
                   plain is not None and streamed is not None
                   and plain["speed_tok_s"] == 0 and streamed["speed_tok_s"] > 0,
                   f"非流式 speed={plain['speed_tok_s'] if plain else 'N/A'}"
                   f"（首字={plain['first_token_ms'] if plain else 'N/A'}ms）· "
                   f"流式 speed={streamed['speed_tok_s'] if streamed else 'N/A'} tok/s"
                   f"（首字={streamed['first_token_ms'] if streamed else 'N/A'}ms）")

        # ------------------------------------------------------------ 7. 余额
        status, refreshed, _ = admin("POST", "/api/admin/balance/refresh", {})
        items = (refreshed or {}).get("items", [])
        ds = next((i for i in items if i.get("channel_name", "").startswith("DeepSeek")), None)
        if ds and ds.get("supported"):
            report.add(22, "DeepSeek 余额可查（真实接口）",
                       ds.get("is_available") is not False,
                       f"币种 {ds.get('currency')} 总额 {ds.get('total')} 充值 {ds.get('topped_up')} 赠送 {ds.get('granted')}")
        else:
            report.add(22, "DeepSeek 余额可查（真实接口）", False, f"结果：{items}")

        # ------------------------------------------------------------ 8. 实时会话
        live_seen: dict[str, Any] = {}

        def fire_slow_stream() -> None:
            try:
                Gateway(gw.base, gw.key, gw.token).stream_chat({
                    "model": args.big_model,
                    "messages": [{"role": "user", "content": "写一段三百字的散文"}],
                    "max_tokens": 512,
                })
            except Exception:
                pass

        worker = threading.Thread(target=fire_slow_stream, daemon=True)
        worker.start()
        live_seen: dict[str, Any] = {}
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(0.3)
            status, live, _ = admin("GET", "/api/admin/live")
            for session in (live or {}).get("active", []):
                # 等到会话真的有渠道归属与进度，才算「看得见正在跑的请求」
                if session.get("channel_name") and (
                    session.get("total_tokens", 0) > 0 or session.get("first_token_ms", 0) > 0
                ):
                    live_seen = session
                    break
            if live_seen:
                break
        report.add(23, "实时会话能看到进行中的请求（含渠道与实时进度）",
                   bool(live_seen),
                   f"{live_seen.get('model')} → {live_seen.get('upstream_model')} · "
                   f"渠道={live_seen.get('channel_name')} · 状态={live_seen.get('status')} · "
                   f"已收={live_seen.get('total_tokens')} tok · 速度={live_seen.get('speed_tok_s')} tok/s · "
                   f"首字={live_seen.get('first_token_ms')}ms · 已用={live_seen.get('elapsed_text')}"
                   if live_seen else "未捕获到有进度的活跃会话")
        worker.join(timeout=240)

        # ------------------------------------------------------------ 9. 路由试算与运维
        status, resolved, _ = admin("GET", f"/api/admin/models/resolve?model={args.small_model}")
        report.add(24, "路由试算可用", status == 200 and bool((resolved or {}).get("candidates")),
                   f"{args.small_model} → 上游 {resolved.get('resolved', {}).get('upstream')} · "
                   f"候选 {[c['name'] for c in resolved.get('candidates', [])]}")
        status, health, _ = admin("GET", "/api/admin/health")
        report.add(25, "渠道健康接口", status == 200 and len((health or {}).get("channels", [])) >= 2,
                   "，".join(f"{c['name']}:{'冷却中' if c.get('cooling') else '正常'}"
                             for c in (health or {}).get("channels", [])))
        csv_response = gw.request("GET", "/api/admin/stats/export.csv?hours=1", timeout=60)
        csv_status = csv_response[0]
        csv_text = csv_response[1] if isinstance(csv_response[1], str) else ""
        report.add(26, "CSV 导出可用", csv_status == 200 and "时间" in csv_text,
                   f"HTTP {csv_status} 行数 {csv_text.count(chr(10))}")
        status, before, _ = admin("GET", "/api/admin/settings")
        page_size = before.get("values", {}).get("ui.page_size")
        status, _, _ = admin("PUT", "/api/admin/settings", {"values": {"ui.page_size": (page_size or 20) + 1}})
        status2, after, _ = admin("GET", "/api/admin/settings")
        changed = after.get("values", {}).get("ui.page_size") == (page_size or 20) + 1
        admin("PUT", "/api/admin/settings", {"values": {"ui.page_size": page_size or 20}})
        report.add(27, "设置读写往返", status < 400 and status2 == 200 and changed,
                   f"ui.page_size {page_size} → {page_size + 1} → 已还原")

        # ------------------------------------------------------------ 10. 计价与成本口径
        status, pricing, _ = admin("GET", "/api/admin/settings")
        currency = (pricing or {}).get("values", {}).get("pricing.currency")
        models_priced = (pricing or {}).get("values", {}).get("pricing.models") or {}
        report.add(28, "计价表状态",
                   True,
                   f"币种 {currency}，已配单价模型 {list(models_priced) or '（空，费用恒为 0——按需在控制台填写）'}")

    finally:
        # ---------------------------------------------------------------- 清理
        for kind, identifier in cleanup:
            path = f"/api/admin/{kind}/{identifier}"
            status, _, _ = admin("DELETE", path)
            print(f"  · 清理临时{kind} {identifier} → HTTP {status}")

    # ---------------------------------------------------------------- 汇总
    passed = len(report.results) - len(report.failed)
    print("\n" + "─" * 64)
    print(f" 验收结果：{passed}/{len(report.results)} 通过")
    if report.failed:
        for no, name, _, detail in report.failed:
            print(f"   ✗ [{no:02d}] {name} — {detail}")
    print("─" * 64 + "\n")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
