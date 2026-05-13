"""LiteLLM provider — single client for 100+ models.

Why LiteLLM and not provider-specific SDKs?

  Each cloud LLM has its own message/tool format, auth scheme, and
  failure modes. [LiteLLM](https://docs.litellm.ai/) is a battle-tested
  unification layer that already speaks all of them; routing happens
  via the model-id prefix:

    anthropic/claude-sonnet-4-5            → Anthropic native
    claude-opus-4-5                        → Anthropic native (bare id)
    openai/gpt-4o   |   gpt-4o             → OpenAI native
    openrouter/anthropic/claude-sonnet-4-5 → OpenRouter
    bedrock/anthropic.claude-3-5-sonnet-…  → AWS Bedrock
    vertex_ai/claude-3-5-sonnet@…          → Google Vertex AI
    gemini/gemini-2.0-flash                → Google Gemini
    groq/llama-3.1-70b-versatile           → Groq
    mistral/mistral-large-latest           → Mistral La Plateforme
    together_ai/meta-llama/Llama-…         → Together
    azure/<deployment-id>                  → Azure OpenAI
    ollama/llama3                          → Local Ollama
    ...                                    (100+ more)

This provider's job is therefore tiny: translate our internal
Anthropic-shaped messages/tools into the OpenAI shape LiteLLM accepts,
call `litellm.acompletion`, and translate the OpenAI-shaped reply back
into Anthropic shape (because the runtime + persisted run state are
built around Anthropic blocks).

The translation helpers live in this module rather than a shared one
because they're an implementation detail of "we go through LiteLLM" —
if some future provider needs a different translation, it lives there.
"""

from __future__ import annotations

import json
import os
from typing import Any


class LiteLLMProvider:
    """Routes every real-LLM call through LiteLLM.

    `__init__` plants PILOTHOUSE_*_API_KEY values into the env vars
    LiteLLM expects (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`,
    `OPENAI_API_KEY`) so the model-id prefix routing works without us
    having to know which key the chosen model needs.

    Per-call we also pass `api_key` / `api_base` / `extra_headers`
    explicitly when the operator has set them — this wins over the
    environment.
    """

    name = "litellm"

    def __init__(
        self,
        *,
        anthropic_api_key: str = "",
        openrouter_api_key: str = "",
        openai_api_key: str = "",
        openai_base_url: str = "",
        openrouter_app_name: str = "Pilothouse",
        openrouter_site_url: str = "",
    ) -> None:
        # Plant the keys LiteLLM picks up via env-var conventions. We
        # only set values we have — never blank out anything the
        # operator put in the real shell env explicitly.
        env_map = {
            "ANTHROPIC_API_KEY": anthropic_api_key,
            "OPENROUTER_API_KEY": openrouter_api_key,
            "OPENAI_API_KEY": openai_api_key,
        }
        for k, v in env_map.items():
            if v:
                os.environ.setdefault(k, v)

        self._openai_base_url = openai_base_url
        self._openrouter_headers: dict[str, str] = {}
        if openrouter_app_name:
            self._openrouter_headers["X-Title"] = openrouter_app_name
        if openrouter_site_url:
            self._openrouter_headers["HTTP-Referer"] = openrouter_site_url

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        # Imported lazily so other callers (e.g. mock-mode tests) don't
        # pay LiteLLM's import-time cost when they don't need it.
        import litellm

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _anthropic_to_openai_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [_anthropic_tool_to_openai(t) for t in tools]

        # Per-provider extras
        if model.startswith("openrouter/") or "/" in model and model.split("/", 1)[0] == "openrouter":
            if self._openrouter_headers:
                kwargs["extra_headers"] = self._openrouter_headers
        if model.startswith("openai/") or _looks_like_openai_native(model):
            if self._openai_base_url:
                kwargs["api_base"] = self._openai_base_url

        response = await litellm.acompletion(**kwargs)
        return _litellm_response_to_anthropic(response)


# --- helpers (module-level so they're unit-testable) ----------------------


def _looks_like_openai_native(model: str) -> bool:
    return (
        "/" not in model
        and (model.startswith("gpt-") or model.startswith("o1-") or model.startswith("o3-"))
    )


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _anthropic_to_openai_messages(
    system: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            out.append({"role": role, "content": str(content)})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for b in content:
                btype = b.get("type")
                if btype == "text":
                    text_parts.append(b.get("text") or "")
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": b.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input") or {}),
                            },
                        }
                    )
            msg: dict[str, Any] = {"role": "assistant"}
            text_joined = "\n".join(p for p in text_parts if p).strip()
            msg["content"] = text_joined or None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        # role == "user" — may carry tool_result blocks mixed with text.
        pending_text: list[str] = []
        for b in content:
            btype = b.get("type")
            if btype == "tool_result":
                if pending_text:
                    out.append(
                        {"role": "user", "content": "\n".join(pending_text).strip()}
                    )
                    pending_text = []
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": _stringify(b.get("content", "")),
                    }
                )
            elif btype == "text":
                pending_text.append(b.get("text") or "")
        if pending_text:
            out.append({"role": "user", "content": "\n".join(pending_text).strip()})

    return out


_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


def _litellm_response_to_anthropic(response: Any) -> dict[str, Any]:
    """Normalize LiteLLM's OpenAI-shaped response into Anthropic shape.

    LiteLLM returns a pydantic-like `ModelResponse`; both `.model_dump()`
    and direct attribute access work. We support both so tests can pass
    plain dicts.
    """
    data: dict[str, Any]
    if hasattr(response, "model_dump"):
        data = response.model_dump()
    elif hasattr(response, "dict"):
        data = response.dict()
    else:
        data = dict(response)

    choices = data.get("choices") or []
    if not choices:
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": ""}],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    msg = choices[0].get("message") or {}
    finish = choices[0].get("finish_reason", "stop")

    content_blocks: list[dict[str, Any]] = []
    text = msg.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments", "{}")
        try:
            input_obj = (
                json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            )
        except json.JSONDecodeError:
            input_obj = {"_unparsed_arguments": raw_args}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": input_obj,
            }
        )

    usage = data.get("usage") or {}
    return {
        "stop_reason": _FINISH_REASON_MAP.get(finish, "end_turn"),
        "content": content_blocks,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    }


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


__all__ = [
    "LiteLLMProvider",
    "_anthropic_tool_to_openai",
    "_anthropic_to_openai_messages",
    "_litellm_response_to_anthropic",
]
