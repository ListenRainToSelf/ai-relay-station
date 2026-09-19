"""适配器单元测试：三协议翻译与流式解析。"""

from __future__ import annotations

import json

import httpx
import pytest

from airelay.adapters import ChatRequest, TokenCounter, create_adapter, estimate_tokens, join_url
from airelay.adapters.media import ImageRequest
from airelay.adapters.anthropic import (
    convert_messages,
    convert_tool_choice,
    convert_tools,
    iter_named_sse,
)
from airelay.adapters.gemini import convert_contents

pytestmark = pytest.mark.anyio


def _async_response(text: str) -> httpx.Response:
    async def body():
        yield text.encode("utf-8")

    return httpx.Response(200, content=body())


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("base", "suffix", "expected"),
    [
        ("https://api.openai.com/v1", "v1/chat/completions", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com", "v1/chat/completions", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com/v1/", "v1/models", "https://api.openai.com/v1/models"),
        ("https://api.anthropic.com", "v1/messages", "https://api.anthropic.com/v1/messages"),
        (
            "https://generativelanguage.googleapis.com/v1beta",
            "v1beta/models/gemini-2.5-flash:generateContent",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        ),
        # 非 v1 的版本段（智谱是 /api/paas/v4）：base 的版本优先
        (
            "https://open.bigmodel.cn/api/paas/v4",
            "v1/chat/completions",
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        ),
        (
            "https://open.bigmodel.cn/api/paas/v4",
            "v1/models",
            "https://open.bigmodel.cn/api/paas/v4/models",
        ),
        # 用户把整条端点 URL 贴进 base_url：先摘端点尾巴，再按 base 的版本拼
        (
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            "v1/chat/completions",
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        ),
        (
            "https://api.deepseek.com/v1/chat/completions",
            "v1/chat/completions",
            "https://api.deepseek.com/v1/chat/completions",
        ),
        # 媒体端点也一样：贴 chat 端点当 base，换端点时仍拼得对
        (
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            "v1/images/generations",
            "https://open.bigmodel.cn/api/paas/v4/images/generations",
        ),
        # base 无版本段：沿用 suffix 自己的版本段
        ("https://open.bigmodel.cn", "v1/models", "https://open.bigmodel.cn/v1/models"),
        ("", "v1/models", "v1/models"),
    ],
)
def test_join_url_dedupes_version_segment(base: str, suffix: str, expected: str) -> None:
    assert join_url(base, suffix) == expected


def test_estimate_tokens_prefers_cjk_weight() -> None:
    assert estimate_tokens("") == 0
    # 中文按字计，英文按 4 字符 1 token
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefgh") == 2


@pytest.mark.parametrize(
    "code, is_cjk",
    [
        (0x2E7F, False), (0x2E80, True), (0x9FFF, True), (0xA000, False),
        (0xABFF, False), (0xAC00, True), (0xD7AF, True), (0xD7B0, False),
        (0xFEFF, False), (0xFF00, True), (0xFFEF, True), (0xFFF0, False),
    ],
)
def test_estimate_tokens_cjk_boundaries(code: int, is_cjk: bool) -> None:
    """估算改成 C 层删除表实现后，区段边界必须与原来逐字符比较完全一致。

    判据用「4 个相同字符」：CJK 按字计 → 4，其余按 4 字符 1 token → 1。
    单个字符两种口径都是 1，区分不出来。
    """
    char = chr(code)
    assert estimate_tokens(char) == 1
    assert estimate_tokens(char * 4) == (4 if is_cjk else 1)


def test_token_counter_matches_full_text_estimate() -> None:
    """增量累加的结果必须与「一次性估算全文」逐位相同——报表口径不能因为优化而漂。"""
    text = "秋天的山林里落叶铺满了小路，风从谷底吹上来带着松脂的气味。code: def f() -> int: return 1." * 40
    counter = TokenCounter()
    for index in range(0, len(text), 7):        # 模拟按块到达，块长还不一样
        counter.add(text[index:index + 7])
    assert counter.tokens == estimate_tokens(text)
    assert counter.chars == len(text)

    empty = TokenCounter()
    empty.add("")
    empty.add(None)
    assert empty.tokens == 0


def test_token_counter_adds_only_new_text() -> None:
    """每块只处理新增文本：扫描量随总长度线性增长，而不是块数 × 全文长度。"""
    counter = TokenCounter()
    piece = "你好世界" * 10
    for _ in range(50):
        counter.add(piece)
    assert counter.chars == len(piece) * 50
    assert counter.tokens == estimate_tokens(piece * 50)


# --------------------------------------------------------------------------- #
# OpenAI 透传
# --------------------------------------------------------------------------- #
def test_openai_adapter_replaces_model_and_keeps_params() -> None:
    adapter = create_adapter("openai", api_key="sk-x", base_url="https://api.openai.com/v1")
    request = ChatRequest.from_body(
        {
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.3,
            "stream": True,
        }
    )
    call = adapter.build_chat_call(request, "gpt-4o-mini", defaults={"max_tokens": 1024})
    assert call.url.endswith("/v1/chat/completions")
    assert call.json_body["model"] == "gpt-4o-mini"
    assert call.json_body["temperature"] == 0.3
    assert call.headers["Authorization"] == "Bearer sk-x"
    # 客户端没要 usage 就不该塞 stream_options（部分兼容上游不认这个字段）
    assert "stream_options" not in call.json_body


def test_openai_adapter_injects_include_usage_only_when_requested() -> None:
    adapter = create_adapter("openai", api_key="sk-x", base_url="https://api.openai.com/v1")
    request = ChatRequest.from_body(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )
    call = adapter.build_chat_call(request, "gpt-4o-mini", defaults={"max_tokens": 1024})
    assert call.json_body["stream_options"] == {"include_usage": True}


def test_deepseek_adapter_balance_url_from_base() -> None:
    adapter = create_adapter("deepseek", api_key="sk-y", base_url="https://api.deepseek.com/v1")
    call = adapter.build_balance_call()
    assert call is not None
    assert call.url == "https://api.deepseek.com/user/balance"
    payload = {
        "is_available": True,
        "balance_infos": [
            {"currency": "USD", "total_balance": "1.00", "granted_balance": "0", "topped_up_balance": "1.00"},
            {"currency": "CNY", "total_balance": "88.5", "granted_balance": "8.5", "topped_up_balance": "80"},
        ],
    }
    normalized = adapter.normalize_balance(payload)
    assert normalized["currency"] == "CNY"
    assert normalized["total"] == 88.5
    assert normalized["granted"] == 8.5


def test_zhipu_adapter_uses_v4_prefix_and_tolerates_pasted_endpoint() -> None:
    """智谱的版本段是 /v4，且用户很可能把整条端点贴进 base_url。"""
    request = ChatRequest.from_body(
        {"model": "glm-4-flash", "messages": [{"role": "user", "content": "hi"}]}
    )
    for base_url in (
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/api/paas/v4/",
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    ):
        adapter = create_adapter("zhipu", api_key="k", base_url=base_url)
        chat = adapter.build_chat_call(request, "glm-4-flash", defaults={"max_tokens": 64})
        assert chat.url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        assert adapter.build_models_call().url == "https://open.bigmodel.cn/api/paas/v4/models"
        images = adapter.build_image_call(
            ImageRequest(model="cogview-3-flash", prompt="一只猫"), "cogview-3-flash", defaults={}
        )
        assert images.url == "https://open.bigmodel.cn/api/paas/v4/images/generations"


def test_zhipu_provider_is_registered_with_preset() -> None:
    from airelay.adapters.registry import normalize_provider, provider_metadata

    assert normalize_provider("glm") == "zhipu"
    assert normalize_provider("zhipuai") == "zhipu"
    assert normalize_provider("bigmodel") == "zhipu"
    entry = next(x for x in provider_metadata() if x["type"] == "zhipu")
    assert entry["label"] == "智谱 GLM"
    assert entry["default_base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert "chat" in entry["capabilities"] and "images" in entry["capabilities"]


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #
def test_anthropic_message_conversion_extracts_system_and_tools() -> None:
    system_text, messages = convert_messages(
        [
            {"role": "system", "content": "你是严谨的助手"},
            {"role": "user", "content": "帮我查天气"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"上海"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "晴 26℃"},
        ]
    )
    assert system_text == "你是严谨的助手"
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    tool_use = messages[1]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["name"] == "get_weather"
    assert tool_use["input"] == {"city": "上海"}
    tool_result = messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "call_1"


def test_anthropic_message_conversion_prepends_user_when_first_is_assistant() -> None:
    _, messages = convert_messages([{"role": "assistant", "content": "先说话"}])
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"


def test_anthropic_merges_adjacent_same_role_messages() -> None:
    _, messages = convert_messages(
        [
            {"role": "user", "content": "第一句"},
            {"role": "user", "content": "第二句"},
        ]
    )
    assert len(messages) == 1
    assert len(messages[0]["content"]) == 2


def test_anthropic_image_data_url_becomes_base64_block() -> None:
    _, messages = convert_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                ],
            }
        ]
    )
    blocks = messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": "看看这张图"}
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}


def test_anthropic_tools_and_choice_conversion() -> None:
    tools = convert_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查天气",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
    )
    assert tools[0]["name"] == "get_weather"
    assert tools[0]["input_schema"]["properties"]["city"]["type"] == "string"
    assert convert_tool_choice("auto") == {"type": "auto"}
    assert convert_tool_choice("required") == {"type": "any"}
    assert convert_tool_choice({"type": "function", "function": {"name": "get_weather"}}) == {
        "type": "tool",
        "name": "get_weather",
    }


def test_anthropic_request_requires_max_tokens_and_names_stop_sequences() -> None:
    adapter = create_adapter("anthropic", api_key="k", base_url="https://api.anthropic.com")
    request = ChatRequest.from_body(
        {
            "model": "claude-4",
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["END"],
        }
    )
    call = adapter.build_chat_call(request, "claude-sonnet-4-5", defaults={"max_tokens": 4096})
    assert call.json_body["max_tokens"] == 4096
    assert call.json_body["stop_sequences"] == ["END"]
    assert call.headers["anthropic-version"] == "2023-06-01"
    assert call.headers["x-api-key"] == "k"


def test_anthropic_normalize_response_maps_text_tools_and_usage() -> None:
    adapter = create_adapter("anthropic", api_key="k", base_url="https://api.anthropic.com")
    request = ChatRequest.from_body({"model": "claude-4", "messages": [{"role": "user", "content": "hi"}]})
    payload = {
        "id": "msg_1",
        "content": [
            {"type": "text", "text": "答案是 "},
            {"type": "tool_use", "id": "toolu_1", "name": "calc", "input": {"a": 1}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 9},
    }
    normalized = adapter.normalize_response(payload, request, "claude-sonnet-4-5")
    choice = normalized["choices"][0]
    assert choice["message"]["content"] == "答案是 "
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "calc"
    assert choice["message"]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert choice["finish_reason"] == "tool_calls"
    assert normalized["model"] == "claude-4"
    assert normalized["usage"] == {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}


async def test_anthropic_stream_translation() -> None:
    adapter = create_adapter("anthropic", api_key="k", base_url="https://api.anthropic.com")
    request = ChatRequest.from_body(
        {"model": "claude-4", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":3}}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"世界"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    chunks = [chunk async for chunk in adapter.iter_stream(_async_response(sse), request, "claude-sonnet-4-5")]
    text = "".join(
        (choice.get("delta") or {}).get("content") or ""
        for chunk in chunks
        for choice in chunk["choices"]
    )
    assert text == "你好世界"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert all(chunk["model"] == "claude-4" for chunk in chunks)


async def test_anthropic_named_sse_parser_handles_ping_and_multiline() -> None:
    sse = (
        ": ping\n\n"
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n'
    )
    events = [item async for item in iter_named_sse(_async_response(sse))]
    assert events[0][0] == "message_delta"
    assert events[0][1]["delta"]["stop_reason"] == "max_tokens"


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
def test_gemini_contents_conversion_and_system_instruction() -> None:
    system_text, contents = convert_contents(
        [
            {"role": "system", "content": "简洁回答"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在的"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}]},
        ]
    )
    assert system_text == "简洁回答"
    assert contents[0] == {"role": "user", "parts": [{"text": "你好"}]}
    assert contents[1]["role"] == "model"
    assert contents[2]["parts"][0]["inlineData"]["mimeType"] == "image/jpeg"


def test_gemini_tool_result_names_function_response() -> None:
    _, contents = convert_contents(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_9", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_9", "content": "结果"},
        ]
    )
    response_part = contents[1]["parts"][0]["functionResponse"]
    assert response_part["name"] == "lookup"
    assert response_part["response"]["content"] == "结果"


def test_gemini_request_uses_v1beta_path_and_alt_sse() -> None:
    adapter = create_adapter("gemini", api_key="g", base_url="https://generativelanguage.googleapis.com")
    request = ChatRequest.from_body(
        {"model": "flash", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    call = adapter.build_chat_call(request, "gemini-2.5-flash", defaults={"max_tokens": 2048})
    assert ":streamGenerateContent" in call.url
    assert call.params == {"alt": "sse"}
    assert call.headers["x-goog-api-key"] == "g"
    assert call.json_body["generationConfig"]["maxOutputTokens"] == 2048


def test_gemini_normalize_response_maps_finish_reason_and_usage() -> None:
    adapter = create_adapter("gemini", api_key="g", base_url="https://generativelanguage.googleapis.com")
    request = ChatRequest.from_body({"model": "flash", "messages": [{"role": "user", "content": "hi"}]})
    payload = {
        "responseId": "resp_1",
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "你好"}]},
                "finishReason": "MAX_TOKENS",
            }
        ],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
    }
    normalized = adapter.normalize_response(payload, request, "gemini-2.5-flash")
    assert normalized["choices"][0]["message"]["content"] == "你好"
    assert normalized["choices"][0]["finish_reason"] == "length"
    assert normalized["usage"]["total_tokens"] == 5
    assert normalized["model"] == "flash"


async def test_gemini_stream_translation() -> None:
    adapter = create_adapter("gemini", api_key="g", base_url="https://generativelanguage.googleapis.com")
    request = ChatRequest.from_body(
        {"model": "flash", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    sse = (
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"甲"}]}}]}\n\n'
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"乙"}]}}]}\n\n'
        'data: {"candidates":[{"content":{"role":"model","parts":[]},"finishReason":"STOP"}],'
        '"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":3,"totalTokenCount":5}}\n\n'
    )
    chunks = [chunk async for chunk in adapter.iter_stream(_async_response(sse), request, "gemini-2.5-flash")]
    text = "".join(
        (choice.get("delta") or {}).get("content") or ""
        for chunk in chunks
        for choice in chunk["choices"]
    )
    assert text == "甲乙"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 5


async def test_gemini_stream_raises_relay_error_on_error_event() -> None:
    from airelay.errors import RelayError

    adapter = create_adapter("gemini", api_key="g", base_url="https://generativelanguage.googleapis.com")
    request = ChatRequest.from_body(
        {"model": "flash", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    sse = 'data: {"error":{"message":"额度不足","code":429}}\n\n'
    with pytest.raises(RelayError) as excinfo:
        async for _ in adapter.iter_stream(_async_response(sse), request, "gemini-2.5-flash"):
            pass
    assert "额度不足" in excinfo.value.message
