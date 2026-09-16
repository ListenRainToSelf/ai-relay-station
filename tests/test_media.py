"""多能力测试：语音合成 / 语音识别 / 图片生成 / 能力路由 / 多模态输入。

假上游同时模拟三种上游形态：
  * OpenAI 原生   → /v1/audio/speech、/v1/audio/transcriptions、/v1/images/generations
  * 小米 MiMo     → 音频能力都藏在 /v1/chat/completions 里（TTS 要 assistant 角色）
  * Gemini        → generateContent 的 responseModalities 决定返回音频/图片
"""

from __future__ import annotations

import base64
import io
import struct
import wave

import pytest
from sqlalchemy import select

from conftest import auth_header, create_channel, create_key
from airelay.models import UsageLog

pytestmark = pytest.mark.anyio

SPEECH = "/v1/audio/speech"
TRANSCRIBE = "/v1/audio/transcriptions"
IMAGES = "/v1/images/generations"
CHAT = "/v1/chat/completions"


def make_wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", 800) for _ in range(int(rate * seconds))))
    return buffer.getvalue()


def is_wav(payload: bytes) -> bool:
    return payload[:4] == b"RIFF" and payload[8:12] == b"WAVE"


# --------------------------------------------------------------------------- #
# 语音合成
# --------------------------------------------------------------------------- #
async def test_speech_native_openai_channel(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="openai-tts", provider_type="openai", base_url=mock_upstream,
                         models=["tts-model"])
    _, key = await create_key(ctx, name="语音", model_allowed=["*"])

    response = await client.post(
        SPEECH, json={"model": "tts-model", "input": "你好，世界", "voice": "alloy"},
        headers=auth_header(key),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")
    assert is_wav(response.content)
    assert response.headers["x-airelay-units"].endswith("character")
    assert response.headers["x-request-id"].startswith("req_")

    await ctx.usage.drain()
    async with ctx.session_factory() as session:
        row = (await session.execute(select(UsageLog).order_by(UsageLog.ts.desc()).limit(1))).scalar_one()
    assert row.unit_kind == "character"
    assert row.units == len("你好，世界")
    assert row.status == "ok"


async def test_speech_through_mimo_chat_translation(client, ctx, mock_upstream, mock_state) -> None:
    """MiMo 的 TTS 要 assistant 角色承载文本 —— 适配器必须翻译对，否则上游 400。"""
    await create_channel(ctx, name="mimo", provider_type="xiaomi-mimo", base_url=mock_upstream,
                         models=["mimo-v2.5-tts"])
    _, key = await create_key(ctx, name="语音2", model_allowed=["*"])

    response = await client.post(
        SPEECH, json={"model": "mimo-v2.5-tts", "input": "合成这句话", "voice": "alloy"},
        headers=auth_header(key),
    )
    assert response.status_code == 200, response.text
    assert is_wav(response.content)

    calls = mock_state.calls_for("/v1/chat/completions")
    assert calls, "TTS 应当被翻译成一次对话请求"
    body = calls[-1]["body"]
    roles = [message.get("role") for message in body["messages"]]
    assert "assistant" in roles, f"MiMo 要求 assistant 消息，实际 {roles}"
    assert body["audio"]["voice"] == "mimo_default", "OpenAI 音色名应被翻译成 MiMo 音色"
    assert any("合成这句话" in str(message.get("content")) for message in body["messages"])


async def test_speech_unknown_voice_returns_400(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="mimo", provider_type="xiaomi-mimo", base_url=mock_upstream,
                         models=["mimo-v2.5-tts"])
    _, key = await create_key(ctx, name="语音3", model_allowed=["*"])
    response = await client.post(
        SPEECH, json={"model": "mimo-v2.5-tts", "input": "x", "voice": "not-a-voice"},
        headers=auth_header(key),
    )
    assert response.status_code == 400
    assert "音色" in response.json()["error"]["message"]


async def test_speech_gemini_pcm_is_wrapped_as_wav(client, ctx, mock_upstream) -> None:
    """Gemini 返回裸 PCM，HTTP 层必须包成 WAV，否则客户端放不出来。"""
    await create_channel(ctx, name="gemini", provider_type="gemini", base_url=mock_upstream,
                         models=["gemini-2.5-flash-tts"])
    _, key = await create_key(ctx, name="语音4", model_allowed=["*"])
    response = await client.post(
        SPEECH, json={"model": "gemini-2.5-flash-tts", "input": "念这句话", "voice": "nova"},
        headers=auth_header(key),
    )
    assert response.status_code == 200, response.text
    assert is_wav(response.content), "PCM 应当被包成 WAV"
    assert response.headers["content-type"].startswith("audio/wav")


async def test_speech_empty_input_returns_400(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, key = await create_key(ctx, name="空")
    response = await client.post(SPEECH, json={"model": "tts-model", "input": ""}, headers=auth_header(key))
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "input"


# --------------------------------------------------------------------------- #
# 语音识别
# --------------------------------------------------------------------------- #
async def test_transcription_native_multipart(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="openai-asr", provider_type="openai", base_url=mock_upstream,
                         models=["asr-model"])
    _, key = await create_key(ctx, name="识别", model_allowed=["*"])
    wav = make_wav(0.5)

    response = await client.post(
        TRANSCRIBE,
        data={"model": "asr-model", "language": "zh"},
        files={"file": ("sample.wav", wav, "audio/wav")},
        headers=auth_header(key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "这是测试上游的回复。" or response.json()["text"]
    assert response.headers["x-airelay-units"].endswith("second")

    calls = mock_state.calls_for("/v1/audio/transcriptions")
    assert calls and calls[-1]["body"]["filename"] == "sample.wav"
    assert calls[-1]["body"]["size"] == len(wav)

    await ctx.usage.drain()
    async with ctx.session_factory() as session:
        row = (await session.execute(select(UsageLog).order_by(UsageLog.ts.desc()).limit(1))).scalar_one()
    assert row.unit_kind == "second"
    assert row.units == 1, f"0.5 秒音频应记 1 秒（四舍五入），实际 {row.units}"


async def test_transcription_mimo_chat_translation(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="mimo-asr", provider_type="xiaomi-mimo", base_url=mock_upstream,
                         models=["mimo-v2.5-asr"])
    _, key = await create_key(ctx, name="识别2", model_allowed=["*"])
    response = await client.post(
        TRANSCRIBE,
        data={"model": "mimo-v2.5-asr"},
        files={"file": ("a.wav", make_wav(0.25), "audio/wav")},
        headers=auth_header(key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"]
    body = mock_state.calls_for("/v1/chat/completions")[-1]["body"]
    parts = [part for message in body["messages"] for part in (message.get("content") or [])
             if isinstance(part, dict)]
    # MiMo 的硬约束：只能有一个 input_audio，且不能夹带 text 块（否则上游 400）
    assert sum(1 for part in parts if part.get("type") == "input_audio") == 1
    assert not [part for part in parts if part.get("type") == "text"], "不能带 text 内容块"
    assert any(part.get("input_audio", {}).get("format") == "wav" for part in parts)


async def test_transcription_mimo_language_is_top_level(client, ctx, mock_upstream, mock_state) -> None:
    """language 只能作为顶层字段传（放进 messages 会被上游拒绝）。"""
    await create_channel(ctx, name="mimo-asr2", provider_type="xiaomi-mimo", base_url=mock_upstream,
                         models=["mimo-v2.5-asr"])
    _, key = await create_key(ctx, name="识别3", model_allowed=["*"])
    response = await client.post(
        TRANSCRIBE,
        data={"model": "mimo-v2.5-asr", "language": "zh"},
        files={"file": ("a.wav", make_wav(0.3), "audio/wav")},
        headers=auth_header(key),
    )
    assert response.status_code == 200, response.text
    body = mock_state.calls_for("/v1/chat/completions")[-1]["body"]
    assert body.get("language") == "zh", "language 应当作为顶层字段"


async def test_transcription_requires_file(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, key = await create_key(ctx, name="无文件")
    response = await client.post(TRANSCRIBE, data={"model": "asr-model"}, headers=auth_header(key))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


# --------------------------------------------------------------------------- #
# 图片生成
# --------------------------------------------------------------------------- #
async def test_images_native_openai(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="openai-img", provider_type="openai", base_url=mock_upstream,
                         models=["img-model"])
    _, key = await create_key(ctx, name="画图", model_allowed=["*"])
    response = await client.post(
        IMAGES, json={"model": "img-model", "prompt": "一只猫", "n": 2}, headers=auth_header(key)
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["data"]) == 2
    assert payload["data"][0]["b64_json"]
    assert response.headers["x-airelay-units"] == "2 image"

    await ctx.usage.drain()
    async with ctx.session_factory() as session:
        row = (await session.execute(select(UsageLog).order_by(UsageLog.ts.desc()).limit(1))).scalar_one()
    assert row.unit_kind == "image" and row.units == 2


async def test_images_gemini_generate_content(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="gemini-img", provider_type="gemini", base_url=mock_upstream,
                         models=["gemini-2.5-flash-image"])
    _, key = await create_key(ctx, name="画图2", model_allowed=["*"])
    response = await client.post(
        IMAGES, json={"model": "gemini-2.5-flash-image", "prompt": "一只狗"}, headers=auth_header(key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"], "inlineData 应被映射成 b64_json"


async def test_images_gemini_imagen_predict(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="gemini-imagen", provider_type="gemini", base_url=mock_upstream,
                         models=["imagen-3.0-generate-002"])
    _, key = await create_key(ctx, name="画图3", model_allowed=["*"])
    response = await client.post(
        IMAGES, json={"model": "imagen-3.0-generate-002", "prompt": "风景", "n": 2},
        headers=auth_header(key),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 2, "imagen 走 :predict，predictions 要能解析"


async def test_images_missing_prompt(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="c", provider_type="openai", base_url=mock_upstream)
    _, key = await create_key(ctx, name="无提示")
    response = await client.post(IMAGES, json={"model": "img-model"}, headers=auth_header(key))
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 能力路由
# --------------------------------------------------------------------------- #
async def test_channel_capabilities_block_unsupported_requests(client, ctx, mock_upstream) -> None:
    """渠道只放开 chat 时，TTS / 图片请求应当被挡下并给出可读原因。"""
    await create_channel(
        ctx, name="only-chat", provider_type="openai", base_url=mock_upstream,
        models=["tts-model", "img-model"], capabilities=["chat"],
    )
    _, key = await create_key(ctx, name="限能力", model_allowed=["*"])

    speech = await client.post(SPEECH, json={"model": "tts-model", "input": "x"}, headers=auth_header(key))
    assert speech.status_code == 503
    assert speech.json()["error"]["code"] == "NO_CHANNEL_AVAILABLE"
    assert "能力" in speech.json()["error"]["message"]

    images = await client.post(IMAGES, json={"model": "img-model", "prompt": "x"}, headers=auth_header(key))
    assert images.status_code == 503


async def test_text_only_protocol_rejects_speech(client, ctx, mock_upstream) -> None:
    """纯文本协议（DeepSeek）默认就没有语音合成能力，TTS 请求应当 503。"""
    await create_channel(
        ctx, name="ds", provider_type="deepseek", base_url=mock_upstream, models=["tts-model"],
    )
    _, key = await create_key(ctx, name="纯文本", model_allowed=["*"])
    response = await client.post(SPEECH, json={"model": "tts-model", "input": "x"}, headers=auth_header(key))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NO_CHANNEL_AVAILABLE"


async def test_speech_falls_back_to_capable_channel(client, ctx, mock_upstream) -> None:
    """多个渠道服务同一模型时，只有具备该能力的那个会被选中。"""
    await create_channel(
        ctx, name="文本渠道", provider_type="openai", base_url=mock_upstream,
        models=["tts-model"], capabilities=["chat"], priority=0,
    )
    await create_channel(
        ctx, name="语音渠道", provider_type="openai", base_url=mock_upstream,
        models=["tts-model"], capabilities=["chat", "speech"], priority=1,
    )
    _, key = await create_key(ctx, name="回退", model_allowed=["*"])
    response = await client.post(
        SPEECH, json={"model": "tts-model", "input": "走语音渠道"}, headers=auth_header(key)
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")


# --------------------------------------------------------------------------- #
# 多模态输入
# --------------------------------------------------------------------------- #
async def test_chat_with_image_requires_vision_capability(client, ctx, mock_upstream) -> None:
    """带图片的对话只能走「有图片输入能力」的渠道。"""
    await create_channel(
        ctx, name="文本渠道", provider_type="openai", base_url=mock_upstream,
        models=["m"], capabilities=["chat"], priority=0,
    )
    visual = await create_channel(
        ctx, name="视觉渠道", provider_type="openai", base_url=mock_upstream,
        models=["m"], capabilities=["chat", "vision"], priority=1,
    )
    _, key = await create_key(ctx, name="多模态", model_allowed=["*"])
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]}],
    }
    response = await client.post(CHAT, json=payload, headers=auth_header(key))
    assert response.status_code == 200
    # 渠道名是中文，HTTP 头里会退回 channel_id（头只能放 latin-1），所以用 id 断言
    assert response.headers["x-airelay-channel-id"] == visual["channel_id"]


async def test_chat_with_image_falls_back_to_text_channel(client, ctx, mock_upstream) -> None:
    """只有纯文本渠道时，带图片的对话也必须能发出去（图片支持只是偏好，不是硬门槛）。

    这是真实踩过的坑：把 vision 当硬门槛会让「上下文里带截图的会话」整条 503，
    客户端表现成「重连中」。
    """
    only_text = await create_channel(
        ctx, name="只有文本", provider_type="deepseek", base_url=mock_upstream,
        models=["m"], capabilities=["chat"],
    )
    _, key = await create_key(ctx, name="无视觉", model_allowed=["*"])
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]}],
    }
    response = await client.post(CHAT, json=payload, headers=auth_header(key))
    assert response.status_code == 200, response.text
    assert response.headers["x-airelay-channel-id"] == only_text["channel_id"]


async def test_chat_with_audio_input_routes_to_audio_capable_channel(client, ctx, mock_upstream) -> None:
    await create_channel(
        ctx, name="文本渠道", provider_type="openai", base_url=mock_upstream,
        models=["m"], capabilities=["chat"], priority=0,
    )
    audio = await create_channel(
        ctx, name="音频渠道", provider_type="openai", base_url=mock_upstream,
        models=["m"], capabilities=["chat", "audio_in"], priority=1,
    )
    _, key = await create_key(ctx, name="音频入", model_allowed=["*"])
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "wav"}},
        ]}],
    }
    response = await client.post(CHAT, json=payload, headers=auth_header(key))
    assert response.status_code == 200
    assert response.headers["x-airelay-channel-id"] == audio["channel_id"]


async def test_gemini_audio_input_is_translated_to_inline_data(client, ctx, mock_upstream, mock_state) -> None:
    await create_channel(ctx, name="gemini", provider_type="gemini", base_url=mock_upstream,
                         models=["gemini-2.5-flash"])
    _, key = await create_key(ctx, name="音频入2", model_allowed=["*"])
    payload = {
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "mp3"}},
        ]}],
    }
    response = await client.post(CHAT, json=payload, headers=auth_header(key))
    assert response.status_code == 200, response.text
    body = mock_state.calls_for("/v1beta/models")[-1]["body"]
    inline = [part["inlineData"] for content in body["contents"] for part in content["parts"]
              if "inlineData" in part]
    assert inline and inline[0]["mimeType"] == "audio/mp3"


# --------------------------------------------------------------------------- #
# 计价与能力发现
# --------------------------------------------------------------------------- #
async def test_media_pricing_uses_character_unit(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="tts", provider_type="openai", base_url=mock_upstream,
                         models=["tts-model"])
    async with ctx.session_factory() as session:
        await ctx.settings.update(session, {"pricing.models": {"tts-model": {"character": 100.0}}})
    _, key = await create_key(ctx, name="计价", model_allowed=["*"])
    text = "一二三四五六七八九十"          # 10 个字符
    await client.post(SPEECH, json={"model": "tts-model", "input": text}, headers=auth_header(key))
    await ctx.usage.drain()
    async with ctx.session_factory() as session:
        row = (await session.execute(select(UsageLog).order_by(UsageLog.ts.desc()).limit(1))).scalar_one()
    # 100 美元/百万字符 × 10 字符 = 0.001 美元 = 1000 µ$
    assert row.cost_units == 1000, f"按字符计价应为 1000 µ$，实际 {row.cost_units}"


async def test_models_endpoint_exposes_capabilities(client, ctx, mock_upstream) -> None:
    await create_channel(ctx, name="mimo", provider_type="xiaomi-mimo", base_url=mock_upstream,
                         models=["mimo-v2.5-tts", "mimo-v2.5-asr"])
    await create_channel(ctx, name="ds", provider_type="deepseek", base_url=mock_upstream,
                         models=["deepseek-chat"])
    _, key = await create_key(ctx, name="看能力", model_allowed=["*"])
    data = (await client.get("/v1/models", headers=auth_header(key))).json()["data"]
    by_id = {item["id"]: item for item in data}
    assert "speech" in by_id["mimo-v2.5-tts"]["capabilities"]
    assert "transcription" in by_id["mimo-v2.5-asr"]["capabilities"]
    assert by_id["deepseek-chat"]["capabilities"] == ["chat"]


async def test_speech_still_hard_gated_for_text_only_channel(client, ctx, mock_upstream) -> None:
    """偏好可以放宽，但专用端点的硬门槛不能放宽：TTS 不该流向纯文本渠道。"""
    await create_channel(
        ctx, name="只有文本", provider_type="deepseek", base_url=mock_upstream,
        models=["m"], capabilities=["chat"],
    )
    _, key = await create_key(ctx, name="硬门槛", model_allowed=["*"])
    response = await client.post(SPEECH, json={"model": "m", "input": "x"}, headers=auth_header(key))
    assert response.status_code == 503
