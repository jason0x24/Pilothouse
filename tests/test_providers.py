"""Tests for the LLM provider abstraction.

After the LiteLLM refactor there are only two providers: `MockProvider`
(no network, deterministic replay) and `LiteLLMProvider` (single client
covering 100+ cloud/self-hosted models). These tests cover:

1. `get_provider()` / `is_mock_mode()` selection logic.
2. Anthropic ↔ OpenAI message + tool + response translation, which is
   how the LiteLLM provider bridges our internal shape with LiteLLM's
   OpenAI-style API.
3. `LiteLLMProvider.complete()` end-to-end with `litellm.acompletion`
   monkey-patched, so a future LiteLLM API drift surfaces in CI.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pilothouse.agent.providers import get_provider, is_mock_mode
from pilothouse.agent.providers.litellm_provider import (
    LiteLLMProvider,
    _anthropic_to_openai_messages,
    _anthropic_tool_to_openai,
    _litellm_response_to_anthropic,
)
from pilothouse.agent.providers.mock_provider import MockProvider
from pilothouse.config import Settings


def _settings(**overrides: Any) -> Settings:
    base = {
        "anthropic_api_key": "",
        "openrouter_api_key": "",
        "openai_api_key": "",
        "openai_base_url": "",
        "openrouter_app_name": "",
        "openrouter_site_url": "",
        "model_provider": "",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clear_third_party_env(monkeypatch):
    """Ensure provider selection isn't affected by the host shell env."""
    for k in (
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GEMINI_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "TOGETHER_API_KEY",
        "COHERE_API_KEY",
        "DEEPSEEK_API_KEY",
        "REPLICATE_API_KEY",
        "PERPLEXITYAI_API_KEY",
        "FIREWORKS_AI_API_KEY",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


# --- selection -------------------------------------------------------------


def test_get_provider_defaults_to_mock_when_no_keys():
    p = get_provider(_settings())
    assert isinstance(p, MockProvider)
    assert is_mock_mode(_settings()) is True


def test_anthropic_key_picks_litellm():
    p = get_provider(_settings(anthropic_api_key="sk-ant-x"))
    assert isinstance(p, LiteLLMProvider)
    assert is_mock_mode(_settings(anthropic_api_key="sk-ant-x")) is False


def test_openrouter_key_picks_litellm():
    p = get_provider(_settings(openrouter_api_key="or-x"))
    assert isinstance(p, LiteLLMProvider)


def test_openai_key_picks_litellm():
    p = get_provider(_settings(openai_api_key="sk-x"))
    assert isinstance(p, LiteLLMProvider)


def test_third_party_env_key_also_counts(monkeypatch):
    # AWS Bedrock / Vertex / Groq / Azure / etc. — LiteLLM picks these up
    # natively; we must not fall through to mock mode just because no
    # PILOTHOUSE_*_API_KEY is set.
    monkeypatch.setenv("GROQ_API_KEY", "gsk_xxx")
    p = get_provider(_settings())
    assert isinstance(p, LiteLLMProvider)
    assert is_mock_mode(_settings()) is False


def test_force_mock_provider():
    p = get_provider(_settings(model_provider="mock", anthropic_api_key="leak"))
    assert isinstance(p, MockProvider)
    assert is_mock_mode(_settings(model_provider="mock")) is True


def test_explicit_litellm_with_no_keys_still_returns_litellm():
    # An operator may have provider-specific env vars LiteLLM picks up
    # that we don't enumerate (e.g. PALM_API_KEY). Don't second-guess
    # them — return LiteLLM and let it 401 if nothing's set.
    p = get_provider(_settings(model_provider="litellm"))
    assert isinstance(p, LiteLLMProvider)


def test_explicit_unknown_provider_raises():
    with pytest.raises(RuntimeError, match="Unknown"):
        get_provider(_settings(model_provider="bedrock"))


# --- translation: Anthropic → OpenAI --------------------------------------


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
    out = _anthropic_to_openai_messages(
        "",
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "noop",
                        "input": {},
                    }
                ],
            }
        ],
    )
    # OpenAI requires content to be null when only tool_calls are present.
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


def test_user_mixed_text_and_tool_result_splits_into_two_messages():
    out = _anthropic_to_openai_messages(
        "",
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "also, here is more context"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_x",
                        "content": "ok",
                        "is_error": False,
                    },
                ],
            }
        ],
    )
    assert out == [
        {"role": "user", "content": "also, here is more context"},
        {"role": "tool", "tool_call_id": "tu_x", "content": "ok"},
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
                        "tool_use_id": "tu_dict",
                        "content": {"items": [1, 2, 3]},
                        "is_error": False,
                    }
                ],
            }
        ],
    )
    assert json.loads(out[0]["content"]) == {"items": [1, 2, 3]}


# --- translation: LiteLLM/OpenAI → Anthropic ------------------------------


def test_response_text_only():
    r = _litellm_response_to_anthropic(
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
    r = _litellm_response_to_anthropic(
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


def test_response_max_tokens_finish_reason():
    r = _litellm_response_to_anthropic(
        {
            "choices": [
                {
                    "message": {"content": "truncated"},
                    "finish_reason": "length",
                }
            ],
            "usage": {},
        }
    )
    assert r["stop_reason"] == "max_tokens"


def test_response_malformed_tool_arguments_does_not_explode():
    # Models occasionally emit invalid JSON in tool args. Surface it
    # under a sentinel key instead of crashing the run.
    r = _litellm_response_to_anthropic(
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


def test_response_accepts_model_dump_objects():
    """LiteLLM returns a pydantic-like ModelResponse, not a plain dict."""

    class _FakeResponse:
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

    r = _litellm_response_to_anthropic(_FakeResponse())
    assert r["content"] == [{"type": "text", "text": "ok"}]


# --- LiteLLMProvider end-to-end via monkey-patched acompletion -----------


async def test_provider_complete_routes_through_litellm(monkeypatch):
    """Drive `LiteLLMProvider.complete` with a stubbed `litellm.acompletion`.

    Verifies:
      - request kwargs use OpenAI schema (system message, tools array,
        max_tokens)
      - model id is passed through unchanged so LiteLLM's prefix
        routing kicks in
      - response is normalized to Anthropic shape
    """
    import litellm

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs):
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

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    p = LiteLLMProvider(openrouter_api_key="sk-or-x", openrouter_app_name="Pilothouse")
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
        model="openrouter/anthropic/claude-sonnet-4-5",
        max_tokens=512,
    )

    assert captured["model"] == "openrouter/anthropic/claude-sonnet-4-5"
    assert captured["max_tokens"] == 512
    assert captured["messages"][0] == {"role": "system", "content": "be brief"}
    assert captured["tools"][0]["function"]["name"] == "search"
    # OpenRouter header attribution applied for openrouter/* model ids:
    assert captured.get("extra_headers", {}).get("X-Title") == "Pilothouse"

    # Round-trips to Anthropic shape:
    assert response["stop_reason"] == "tool_use"
    assert response["content"][0]["name"] == "search"
    assert response["content"][0]["input"] == {"q": "x"}
    assert response["usage"] == {"input_tokens": 11, "output_tokens": 5}


async def test_provider_omits_extra_headers_for_non_openrouter_models(monkeypatch):
    import litellm

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs):
        captured.update(kwargs)

        class _Resp:
            def model_dump(self) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "message": {"content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    p = LiteLLMProvider(anthropic_api_key="sk-ant-x", openrouter_site_url="x")
    await p.complete(
        system="",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        model="anthropic/claude-sonnet-4-5",
        max_tokens=64,
    )
    # OpenRouter-specific headers must not leak into Anthropic native calls.
    assert "extra_headers" not in captured


async def test_provider_applies_openai_base_url_for_openai_native(monkeypatch):
    """Operator points PILOTHOUSE_OPENAI_BASE_URL at a self-hosted vLLM /
    LM Studio — that should reach LiteLLM as `api_base`."""
    import litellm

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs):
        captured.update(kwargs)

        class _Resp:
            def model_dump(self) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "message": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {},
                }

        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    p = LiteLLMProvider(
        openai_api_key="sk-local",
        openai_base_url="http://127.0.0.1:8000/v1",
    )
    await p.complete(
        system="",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        model="gpt-4o",
        max_tokens=64,
    )
    assert captured["api_base"] == "http://127.0.0.1:8000/v1"


def test_provider_plants_keys_into_env(monkeypatch):
    """LiteLLMProvider must seed the env vars LiteLLM auto-detects so
    model-id prefix routing works without per-call `api_key`."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    LiteLLMProvider(
        anthropic_api_key="sk-ant-test",
        openrouter_api_key="or-test",
        openai_api_key="sk-test",
    )

    import os

    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test"
    assert os.environ.get("OPENROUTER_API_KEY") == "or-test"
    assert os.environ.get("OPENAI_API_KEY") == "sk-test"


def test_provider_respects_pre_existing_env(monkeypatch):
    """If the operator has ANTHROPIC_API_KEY set in their shell already
    (no PILOTHOUSE_ prefix), don't overwrite it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-shell-key")
    LiteLLMProvider(anthropic_api_key="pilothouse-config-key")
    import os

    # `os.environ.setdefault` semantics: shell wins.
    assert os.environ["ANTHROPIC_API_KEY"] == "real-shell-key"
