"""Tests for the LLM provider abstraction.

Three layers:

1. Registry / factory / auto-detect logic in `providers/__init__.py`.
2. Pure Anthropic ↔ OpenAI message + tool + response translation
   (helpers in `openai_compat.py`).
3. End-to-end: each real provider's `.complete()` driven against a
   stubbed SDK client so a future API drift surfaces in CI.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pilothouse.agent.providers import (
    PROVIDER_FACTORIES,
    get_provider,
    is_mock_mode,
    supported_providers,
)
from pilothouse.agent.providers.anthropic_provider import AnthropicProvider
from pilothouse.agent.providers.mock_provider import MockProvider
from pilothouse.agent.providers.openai_compat import (
    OpenAICompatProvider,
    OpenAIProvider,
    OpenRouterProvider,
    _anthropic_to_openai_messages,
    _anthropic_tool_to_openai,
    _openai_response_to_anthropic,
)
from pilothouse.config import Settings


def _settings(**overrides: Any) -> Settings:
    base = {
        "anthropic_api_key": "",
        "openai_api_key": "",
        "openrouter_api_key": "",
        "openai_base_url": "",
        "openrouter_app_name": "",
        "openrouter_site_url": "",
        "model_provider": "",
    }
    base.update(overrides)
    return Settings(**base)


# --- registry / factory --------------------------------------------------


def test_supported_providers_lists_anthropic_openai_openrouter_mock():
    assert set(supported_providers()) == {"anthropic", "openai", "openrouter", "mock"}


def test_registry_keys_match_supported_providers():
    # The factory dict is the single source of truth for which providers
    # exist. Adding a new provider means adding one row here.
    assert set(PROVIDER_FACTORIES) == set(supported_providers())


def test_no_keys_falls_back_to_mock():
    p = get_provider(_settings())
    assert isinstance(p, MockProvider)
    assert is_mock_mode(_settings()) is True


def test_anthropic_key_auto_selects_anthropic():
    p = get_provider(_settings(anthropic_api_key="sk-ant-x"))
    assert isinstance(p, AnthropicProvider)


def test_openai_key_auto_selects_openai():
    p = get_provider(_settings(openai_api_key="sk-x"))
    assert isinstance(p, OpenAIProvider)


def test_openrouter_key_auto_selects_openrouter():
    p = get_provider(_settings(openrouter_api_key="sk-or-x"))
    assert isinstance(p, OpenRouterProvider)


def test_anthropic_wins_auto_detect_when_multiple_keys_set():
    p = get_provider(
        _settings(
            anthropic_api_key="a",
            openrouter_api_key="o",
            openai_api_key="i",
        )
    )
    assert isinstance(p, AnthropicProvider)


def test_explicit_provider_overrides_auto():
    p = get_provider(
        _settings(
            anthropic_api_key="a",
            openrouter_api_key="o",
            model_provider="openrouter",
        )
    )
    assert isinstance(p, OpenRouterProvider)


def test_explicit_provider_without_credential_raises_clearly():
    with pytest.raises(RuntimeError, match="PILOTHOUSE_OPENROUTER_API_KEY"):
        get_provider(_settings(model_provider="openrouter"))


def test_explicit_unknown_provider_raises():
    with pytest.raises(RuntimeError, match="Unknown"):
        get_provider(_settings(model_provider="bedrock"))


def test_force_mock_overrides_real_keys():
    p = get_provider(_settings(model_provider="mock", anthropic_api_key="leak"))
    assert isinstance(p, MockProvider)
    assert is_mock_mode(_settings(model_provider="mock")) is True


def test_is_mock_mode_false_for_explicit_real_provider_even_without_keys():
    # Caller chose a real provider explicitly — they own the failure
    # mode. We don't silently downgrade to mock.
    assert is_mock_mode(_settings(model_provider="openai")) is False


# --- translation: Anthropic → OpenAI -------------------------------------


def test_tool_schema_translation():
    out = _anthropic_tool_to_openai(
        {
            "name": "search",
            "description": "Search the docs.",
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
    )
    assert out == {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the docs.",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }


def test_system_prompt_becomes_first_message():
    out = _anthropic_to_openai_messages(
        "You are a triage agent.",
        [{"role": "user", "content": "hello"}],
    )
    assert out[0] == {"role": "system", "content": "You are a triage agent."}
    assert out[1] == {"role": "user", "content": "hello"}


def test_assistant_tool_use_becomes_tool_calls_with_string_arguments():
    out = _anthropic_to_openai_messages(
        "",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me look that up."},
                    {
                        "type": "tool_use",
                        "id": "toolu_001",
                        "name": "search",
                        "input": {"q": "checkout"},
                    },
                ],
            }
        ],
    )
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Let me look that up."
    assert msg["tool_calls"] == [
        {
            "id": "toolu_001",
            "type": "function",
            "function": {
                "name": "search",
                "arguments": json.dumps({"q": "checkout"}),
            },
        }
    ]


def test_assistant_only_tool_use_has_null_content():
    # OpenAI requires content to be null when only tool_calls are present.
    out = _anthropic_to_openai_messages(
        "",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "noop", "input": {}}
                ],
            }
        ],
    )
    assert out[0]["content"] is None


def test_user_tool_result_becomes_tool_role_message():
    out = _anthropic_to_openai_messages(
        "",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_001",
                        "content": "found 3 results",
                        "is_error": False,
                    }
                ],
            }
        ],
    )
    assert out == [
        {"role": "tool", "tool_call_id": "toolu_001", "content": "found 3 results"}
    ]


def test_tool_result_with_dict_content_is_json_stringified():
    out = _anthropic_to_openai_messages(
        "",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu",
                        "content": {"items": [1, 2, 3]},
                        "is_error": False,
                    }
                ],
            }
        ],
    )
    assert json.loads(out[0]["content"]) == {"items": [1, 2, 3]}


# --- translation: OpenAI → Anthropic -------------------------------------


def test_response_text_only():
    r = _openai_response_to_anthropic(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello world."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
        }
    )
    assert r == {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Hello world."}],
        "usage": {"input_tokens": 42, "output_tokens": 7},
    }


def test_response_with_tool_calls():
    r = _openai_response_to_anthropic(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_xyz",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"q": "checkout"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
    )
    assert r["stop_reason"] == "tool_use"
    assert r["content"] == [
        {
            "type": "tool_use",
            "id": "call_xyz",
            "name": "search",
            "input": {"q": "checkout"},
        }
    ]


def test_response_length_finish_reason_maps_to_max_tokens():
    r = _openai_response_to_anthropic(
        {
            "choices": [
                {"message": {"content": "truncated"}, "finish_reason": "length"}
            ],
            "usage": {},
        }
    )
    assert r["stop_reason"] == "max_tokens"


def test_response_malformed_tool_arguments_does_not_explode():
    # Models occasionally emit invalid JSON in tool args. Surface it
    # under a sentinel key instead of crashing the run.
    r = _openai_response_to_anthropic(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "x",
                                    "arguments": "{not json",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
    )
    assert r["content"][0]["input"] == {"_unparsed_arguments": "{not json"}


def test_response_accepts_pydantic_like_objects():
    class _FakeResp:
        def model_dump(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    r = _openai_response_to_anthropic(_FakeResp())
    assert r["content"] == [{"type": "text", "text": "ok"}]


# --- AnthropicProvider end-to-end via stubbed SDK ------------------------


async def test_anthropic_provider_passes_messages_unchanged():
    p = AnthropicProvider(api_key="sk-ant-x")
    # Replace the underlying SDK with an AsyncMock to verify call shape.
    fake_response = MagicMock()
    fake_response.content = [
        MagicMock(model_dump=lambda: {"type": "text", "text": "Hi"})
    ]
    fake_response.stop_reason = "end_turn"
    fake_response.usage = MagicMock(input_tokens=5, output_tokens=3)
    p._client.messages = MagicMock(create=AsyncMock(return_value=fake_response))

    result = await p.complete(
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "x", "description": "", "input_schema": {}}],
        model="claude-opus-4-5",
        max_tokens=128,
    )

    p._client.messages.create.assert_awaited_once()
    kwargs = p._client.messages.create.await_args.kwargs
    # Anthropic native — no translation, passed straight through.
    assert kwargs["model"] == "claude-opus-4-5"
    assert kwargs["system"] == "be brief"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["max_tokens"] == 128
    assert result["stop_reason"] == "end_turn"
    assert result["content"] == [{"type": "text", "text": "Hi"}]
    assert result["usage"] == {"input_tokens": 5, "output_tokens": 3}


# --- OpenAI / OpenRouter providers end-to-end via stubbed SDK ----------


async def test_openai_compat_translates_request_and_response(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)

        class _Resp:
            def model_dump(self) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_42",
                                        "type": "function",
                                        "function": {
                                            "name": "search",
                                            "arguments": '{"q":"x"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5},
                }

        return _Resp()

    p = OpenAIProvider(api_key="sk-x")
    p._client.chat = MagicMock(
        completions=MagicMock(create=_fake_create)
    )

    response = await p.complete(
        system="be brief",
        messages=[{"role": "user", "content": "search for checkout"}],
        tools=[
            {
                "name": "search",
                "description": "Search.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        model="gpt-4o",
        max_tokens=512,
    )

    # Request was translated to OpenAI shape.
    assert captured["model"] == "gpt-4o"
    assert captured["max_tokens"] == 512
    assert captured["messages"][0] == {"role": "system", "content": "be brief"}
    assert captured["tools"][0]["function"]["name"] == "search"

    # Response was translated back to Anthropic shape.
    assert response["stop_reason"] == "tool_use"
    assert response["content"][0]["name"] == "search"
    assert response["content"][0]["input"] == {"q": "x"}


def test_openrouter_provider_sets_attribution_headers():
    """X-Title and HTTP-Referer must be forwarded to the underlying SDK."""
    p = OpenRouterProvider(
        api_key="sk-or-x",
        app_name="Pilothouse",
        site_url="https://example.com",
    )
    headers = p._client.default_headers  # type: ignore[attr-defined]
    # The SDK may add its own headers; we just need ours to be present.
    assert headers.get("X-Title") == "Pilothouse"
    assert headers.get("HTTP-Referer") == "https://example.com"


def test_openrouter_provider_strips_empty_headers():
    p = OpenRouterProvider(api_key="sk-or-x", app_name="", site_url="")
    headers = p._client.default_headers  # type: ignore[attr-defined]
    assert "X-Title" not in headers
    assert "HTTP-Referer" not in headers


def test_openai_provider_supports_base_url_override():
    """Self-hosted vLLM / LM Studio / Ollama use OpenAI-compat endpoints."""
    p = OpenAIProvider(api_key="sk-local", base_url="http://127.0.0.1:8000/v1")
    # SDK stores base_url; we don't assume exact form (trailing slash, etc.)
    assert "127.0.0.1:8000" in str(p._client.base_url)


def test_constructor_requires_api_key():
    with pytest.raises(ValueError, match="api_key"):
        AnthropicProvider(api_key="")
    with pytest.raises(ValueError, match="api_key"):
        OpenAIProvider(api_key="")
    with pytest.raises(ValueError, match="api_key"):
        OpenRouterProvider(api_key="")
    with pytest.raises(ValueError, match="api_key"):
        OpenAICompatProvider(api_key="", base_url="http://x")
