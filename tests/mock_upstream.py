"""测试用的假上游：同时模拟 OpenAI / Anthropic / Gemini 三种协议。

既可当脚本起（`python tests/mock_upstream.py --port 9001`），
也可被 pytest 直接 import 起来跑。行为可通过 /control/* 动态改变，
用来验证路由重试、故障切换、超时与余额查询。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import struct
import time
import wave
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse



def _tiny_wav(seconds: float = 0.25, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(2000 * __import__("math").sin(2 * 3.141592653589793 * 440 * i / rate)))
            for i in range(int(rate * seconds))
        )
        handle.writeframes(frames)
    return buffer.getvalue()


def _tiny_pcm(seconds: float = 0.2, rate: int = 24000) -> bytes:
    """Gemini TTS 那样返回裸 PCM（没有 WAV 头），网关需要自己包壳。"""
    return b"".join(struct.pack("<h", 1200) for _ in range(int(rate * seconds)))


def _tiny_png() -> bytes:
    """1x1 的透明 PNG，够用来验证图片链路。"""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AARAAB/wD0AQAA"
        "AABJRU5ErkJggg=="
    )


RESULT_TEXT = "这是测试上游的回复。"
TRANSCRIPT_TEXT = "这是测试转写文本。"

REPLY_TEXT = "你好，这里是测试上游的回复。"
PROMPT_TOKENS = 11
COMPLETION_TOKENS = 7

STATE: dict[str, Any] = {
    "calls": [],
    "fail_times": 0,
    "fail_status": 500,
    "fail_paths": [],
    "delay": 0.0,
    "stream_delay": 0.0,
    "stream_chunks": 3,
    "require_assistant_for_tts": True,   # 模拟 MiMo：TTS 必须把文本放 assistant
    "tts_voice_validator": True,          # 音色不在清单里就 400
    "valid_voices": ["mimo_default", "冰糖", "茉莉"],
    "balance": {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "88.50",
                "granted_balance": "8.50",
                "topped_up_balance": "80.00",
            }
        ],
    },
}


def reset_state() -> None:
    STATE.update(
        {
            "calls": [],
            "fail_times": 0,
            "fail_status": 500,
            "fail_paths": [],
            "delay": 0.0,
            "stream_delay": 0.0,
            "stream_chunks": 3,
        }
    )


def calls_for(path_suffix: str) -> list[dict[str, Any]]:
    return [call for call in STATE["calls"] if call["path"].endswith(path_suffix)]


def create_app() -> FastAPI:
    app = FastAPI(title="mock upstream")

    # ------------------------------------------------------------------ 控制面
    @app.post("/control/reset")
    async def control_reset() -> dict[str, Any]:
        reset_state()
        return {"ok": True}

    @app.post("/control/config")
    async def control_config(request: Request) -> dict[str, Any]:
        payload = await request.json()
        STATE.update(payload)
        return {"ok": True, "state": {k: v for k, v in STATE.items() if k != "calls"}}

    @app.get("/control/calls")
    async def control_calls() -> dict[str, Any]:
        return {"calls": STATE["calls"], "total": len(STATE["calls"])}

    # ------------------------------------------------------------------ 通用
    async def _gate(path: str, body: Any) -> JSONResponse | None:
        STATE["calls"].append({"path": path, "body": body, "ts": time.time()})
        if STATE["delay"]:
            await asyncio.sleep(float(STATE["delay"]))
        failing = STATE["fail_times"] > 0 and (
            not STATE["fail_paths"]
            or any(path.endswith(suffix) for suffix in STATE["fail_paths"])
        )
        if failing:
            STATE["fail_times"] = int(STATE["fail_times"]) - 1
            return JSONResponse(
                status_code=int(STATE["fail_status"]),
                content={"error": {"message": "上游假装挂了", "type": "server_error", "code": "mock_failure"}},
            )
        return None

    # ------------------------------------------------------------------ OpenAI
    @app.post("/v1/chat/completions")
    async def openai_chat(request: Request):
        body = await request.json()
        failed = await _gate("/v1/chat/completions", body)
        if failed is not None:
            return failed
        model = body.get("model", "mock-model")

        # 模拟 MiMo：语音合成模型要求在 assistant 消息里给文本，返回 base64 WAV
        if "tts" in model:
            messages = body.get("messages") or []
            assistant = next((m for m in messages if m.get("role") == "assistant"), None)
            if STATE.get("require_assistant_for_tts") and assistant is None:
                return JSONResponse(status_code=400, content={
                    "error": {"code": "400", "message": "Param Incorrect",
                              "param": "messages must contain an assistant role for TTS model"},
                })
            audio_cfg = body.get("audio") or {}
            voice = str(audio_cfg.get("voice") or "")
            if voice and voice not in STATE.get("valid_voices", []):
                return JSONResponse(status_code=400, content={
                    "error": {"code": "400", "message": "Param Incorrect",
                              "param": f"Unknown voice: {voice}. Available voices: {STATE.get('valid_voices')}"},
                })
            encoded = base64.b64encode(_tiny_wav()).decode()
            return JSONResponse({
                "id": "chatcmpl-mock-tts",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": "",
                    "audio": {"id": "audio_mock", "data": encoded, "format": "wav", "transcript": None},
                }}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            })

        # 模拟 MiMo：语音识别模型吃 input_audio 内容块，把转写放在 content 里
        if "asr" in model:
            messages = body.get("messages") or []
            parts = [
                part
                for message in messages
                for part in (message.get("content") or [])
                if isinstance(message.get("content"), list)
                for part in [part]
                if isinstance(part, dict)
            ]
            audio_parts = [part for part in parts if part.get("type") == "input_audio"]
            text_parts = [part for part in parts if part.get("type") == "text"]
            # 与真实 MiMo 一致：不能带 text 块，且只能有一个音频块
            if text_parts:
                return JSONResponse(status_code=400, content={"error": {
                    "code": "400", "message": "Param Incorrect",
                    "param": "ASR request must not include text parts; text prompt is injected by the server",
                }})
            if len(audio_parts) != 1:
                return JSONResponse(status_code=400, content={"error": {
                    "code": "400", "message": "Param Incorrect",
                    "param": f"ASR requires exactly one input_audio part, found: {len(audio_parts)}",
                }})
            return JSONResponse({
                "id": "chatcmpl-mock-asr",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": TRANSCRIPT_TEXT}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
            })

        if body.get("stream"):
            return StreamingResponse(
                _openai_stream(model, body), media_type="text/event-stream"
            )
        return JSONResponse(_openai_payload(model, body))

    @app.get("/v1/models")
    async def openai_models():
        return {
            "object": "list",
            "data": [
                {"id": "mock-gpt-large", "object": "model", "owned_by": "mock"},
                {"id": "mock-gpt-small", "object": "model", "owned_by": "mock"},
            ],
        }

    @app.get("/user/balance")
    async def deepseek_balance(request: Request):
        STATE["calls"].append({"path": "/user/balance", "body": None, "ts": time.time()})
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": {"message": "缺少 Bearer"}})
        return JSONResponse(STATE["balance"])


    # ------------------------------------------------------------------ 语音合成
    @app.post("/v1/audio/speech")
    async def openai_speech(request: Request):
        body = await request.json()
        failed = await _gate("/v1/audio/speech", body)
        if failed is not None:
            return failed
        audio = _tiny_wav()
        return Response(content=audio, media_type="audio/wav")

    # ------------------------------------------------------------------ 语音识别
    @app.post("/v1/audio/transcriptions")
    async def openai_transcriptions(request: Request):
        form = await request.form()
        upload = form.get("file")
        size = 0
        filename = ""
        if upload is not None and hasattr(upload, "read"):
            payload = await upload.read()
            size = len(payload)
            filename = getattr(upload, "filename", "") or ""
        STATE["calls"].append({
            "path": "/v1/audio/transcriptions",
            "body": {"model": str(form.get("model") or ""), "language": str(form.get("language") or ""),
                     "filename": filename, "size": size},
            "ts": time.time(),
        })
        return JSONResponse({"text": TRANSCRIPT_TEXT})

    # ------------------------------------------------------------------ 图片生成
    @app.post("/v1/images/generations")
    async def openai_images(request: Request):
        body = await request.json()
        failed = await _gate("/v1/images/generations", body)
        if failed is not None:
            return failed
        count = int(body.get("n") or 1)
        encoded = base64.b64encode(_tiny_png()).decode()
        return JSONResponse({
            "created": int(time.time()),
            "data": [{"b64_json": encoded, "revised_prompt": body.get("prompt")} for _ in range(count)],
        })

    # ------------------------------------------------------------------ Anthropic
    @app.post("/v1/messages")
    async def anthropic_messages(request: Request):
        body = await request.json()
        failed = await _gate("/v1/messages", body)
        if failed is not None:
            return failed
        if not request.headers.get("x-api-key"):
            return JSONResponse(status_code=401, content={"error": {"message": "缺少 x-api-key"}})
        model = body.get("model", "mock-claude")
        if body.get("stream"):
            return StreamingResponse(
                _anthropic_stream(model), media_type="text/event-stream"
            )
        return JSONResponse(_anthropic_payload(model))

    # ------------------------------------------------------------------ Gemini
    @app.post("/v1beta/models/{model_path:path}")
    async def gemini_generate(model_path: str, request: Request):
        body = await request.json()
        failed = await _gate("/v1beta/models", body)
        if failed is not None:
            return failed
        if not request.headers.get("x-goog-api-key"):
            return JSONResponse(status_code=401, content={"error": {"message": "缺少 x-goog-api-key"}})
        model = model_path.split(":")[0]
        if ":streamGenerateContent" in model_path:
            return StreamingResponse(_gemini_stream(model), media_type="text/event-stream")
        # 注意：{model_path:path} 是贪婪匹配，:predict 请求也会落到这里，
        # 所以 imagen 的 predict 必须在同一个处理函数里分流。
        if ":predict" in model_path:
            count = int(((body.get("parameters") or {}).get("sampleCount")) or 1)
            encoded = base64.b64encode(_tiny_png()).decode()
            return JSONResponse({"predictions": [{"bytesBase64Encoded": encoded} for _ in range(count)]})

        config = body.get("generationConfig") or {}
        modalities = [str(item).upper() for item in (config.get("responseModalities") or [])]
        if "AUDIO" in modalities:
            # 与真实 Gemini 一致：返回裸 PCM，交由网关包成 WAV
            return JSONResponse({
                "candidates": [{"content": {"role": "model", "parts": [{
                    "inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000",
                                   "data": base64.b64encode(_tiny_pcm()).decode()},
                }]}, "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 0, "totalTokenCount": 8},
            })
        if "IMAGE" in modalities:
            return JSONResponse({
                "candidates": [{"content": {"role": "model", "parts": [
                    {"text": ""},
                    {"inlineData": {"mimeType": "image/png",
                                    "data": base64.b64encode(_tiny_png()).decode()}},
                ]}, "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 0, "totalTokenCount": 9},
            })
        # 带音频输入时当作转写请求
        has_audio = any(
            isinstance(part, dict) and (part.get("inlineData") or {}).get("mimeType", "").startswith("audio/")
            for content in body.get("contents") or []
            for part in content.get("parts") or []
            if isinstance(part, dict)
        )
        if has_audio:
            return JSONResponse({
                "candidates": [{"content": {"role": "model", "parts": [{"text": TRANSCRIPT_TEXT}]},
                                "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 15, "candidatesTokenCount": 6, "totalTokenCount": 21},
            })
        return JSONResponse(_gemini_payload(model))



    @app.get("/v1beta/models")
    async def gemini_models():
        return {
            "models": [
                {"name": "models/mock-gemini-pro", "displayName": "Mock Gemini Pro"},
                {"name": "models/mock-gemini-flash", "displayName": "Mock Gemini Flash"},
            ]
        }

    return app


# --------------------------------------------------------------------------- #
# 三种协议的报文构造
# --------------------------------------------------------------------------- #
def _openai_payload(model: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-mock-1",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": REPLY_TEXT},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
        },
    }


async def _openai_stream(model: str, body: dict[str, Any]):
    chunks = int(STATE.get("stream_chunks") or 3)
    pieces = _split_reply(chunks)

    def frame(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    yield frame(
        {
            "id": "chatcmpl-mock-1",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
    )
    for piece in pieces:
        if STATE.get("stream_delay"):
            await asyncio.sleep(float(STATE["stream_delay"]))
        yield frame(
            {
                "id": "chatcmpl-mock-1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
        )
    yield frame(
        {
            "id": "chatcmpl-mock-1",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    options = body.get("stream_options") or {}
    if isinstance(options, dict) and options.get("include_usage"):
        yield frame(
            {
                "id": "chatcmpl-mock-1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": PROMPT_TOKENS,
                    "completion_tokens": COMPLETION_TOKENS,
                    "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                },
            }
        )
    yield b"data: [DONE]\n\n"


def _anthropic_payload(model: str) -> dict[str, Any]:
    return {
        "id": "msg_mock_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": REPLY_TEXT}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": PROMPT_TOKENS, "output_tokens": COMPLETION_TOKENS},
    }


async def _anthropic_stream(model: str):
    def event(name: str, payload: dict[str, Any]) -> bytes:
        return (
            f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ).encode("utf-8")

    yield event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_mock_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": PROMPT_TOKENS, "output_tokens": 0},
            },
        },
    )
    yield event(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    for piece in _split_reply(int(STATE.get("stream_chunks") or 3)):
        if STATE.get("stream_delay"):
            await asyncio.sleep(float(STATE["stream_delay"]))
        yield event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": piece},
            },
        )
    yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": COMPLETION_TOKENS},
        },
    )
    yield event("message_stop", {"type": "message_stop"})


def _gemini_payload(model: str) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": REPLY_TEXT}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": PROMPT_TOKENS,
            "candidatesTokenCount": COMPLETION_TOKENS,
            "totalTokenCount": PROMPT_TOKENS + COMPLETION_TOKENS,
        },
    }


async def _gemini_stream(model: str):
    def frame(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    for piece in _split_reply(int(STATE.get("stream_chunks") or 3)):
        if STATE.get("stream_delay"):
            await asyncio.sleep(float(STATE["stream_delay"]))
        yield frame(
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": piece}]}, "index": 0}
                ]
            }
        )
    yield frame(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": []},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": PROMPT_TOKENS,
                "candidatesTokenCount": COMPLETION_TOKENS,
                "totalTokenCount": PROMPT_TOKENS + COMPLETION_TOKENS,
            },
        }
    )


def _split_reply(count: int) -> list[str]:
    """把回复切成 count 片，保证拼回来与原文完全一致。"""
    count = max(1, min(int(count), len(REPLY_TEXT)))
    size = -(-len(REPLY_TEXT) // count)  # 向上取整
    return [REPLY_TEXT[i : i + size] for i in range(0, len(REPLY_TEXT), size)]


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="假上游服务，用于联调与测试")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
