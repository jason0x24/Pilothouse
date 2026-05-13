"""OpenAI-compatible provider — single class for OpenAI and OpenRouter.

The OpenAI Chat Completions API is the de-facto standard. OpenAI
itself and OpenRouter both speak it; only the `base_url` differs.
This module owns the translation between our internal Anthropic-shaped
content blocks and OpenAI's request/response shape.

The two specific providers (`OpenAIProvider`, `OpenRouterProvider`)
are thin subclasses that set the appropriate `base_url` + default
headers. Adding another OpenAI-compat backend (Groq, Together,
self-hosted vLLM, ...) is a 3-line subclass.

Translation rules
-----------------

Anthropic tool schema:
    {"name", "description", "input_schema": {JSON schema}}
                                ↓
OpenAI tool schema:
    {"type": "function",
     "function": {"name", "description", "parameters": {JSON schema}}}

Anthropic assistant content (list of blocks): text + tool_use blocks
                                ↓
OpenAI assistant message: {"content": "...", "tool_calls": [...]}

Anthropic user content (tool_result blocks): each tool_result becomes
a separate role-"tool" message in OpenAI.

OpenAI finish_reason → Anthropic stop_reason:
    stop           → end_turn
    tool_calls     → tool_use
    function_call  → tool_use (legacy)
    length         → max_tokens
    *              → end_turn
"""

from __future__ import annotations

import json
from typing import Any

from .base import LLMProvider


_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


class OpenAICompatProvider:
    """Base class for any OpenAI Chat Completions-compatible backend.

    Subclasses or callers supply (api_key, base_url, default_headers).
    """

    name = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        name: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(f"{type(self).__name__} requires an api_key")
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if default_headers:
            kwargs["default_headers"] = {
                k: v for k, v in default_headers.items() if v
            }
        self._client = AsyncOpenAI(**kwargs)
        if name:
            self.name = name

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        openai_messages = _anthropic_to_openai_messages(system, messages)
        openai_tools = [_anthropic_tool_to_openai(t) for t in tools] if tools else None

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
        }
        if openai_tools:
            call_kwargs["tools"] = openai_tools

        response = await self._client.chat.completions.create(**call_kwargs)
        return _openai_response_to_anthropic(response)


class OpenAIProvider(OpenAICompatProvider):
    """OpenAI native (`api.openai.com`) — also accepts an `api_base`
    override for self-hosted OpenAI-compat endpoints (vLLM, LM Studio)."""

    name = "openai"

    def __init__(self, *, api_key: str, base_url: str = "") -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or None,  # SDK default: api.openai.com
            name="openai",
        )


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter — same OpenAI Chat Completions API at a different host.

    OpenRouter uses two optional headers for app-level attribution
    and rate-limit tiers:
      - X-Title:     a human-readable name
      - HTTP-Referer: a URL identifying your app
    """

    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        app_name: str = "Pilothouse",
        site_url: str = "",
    ) -> None:
        headers: dict[str, str] = {}
        if app_name:
            headers["X-Title"] = app_name
        if site_url:
            headers["HTTP-Referer"] = site_url
        super().__init__(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers=headers,
            name="openrouter",
        )


# --- translation helpers (module-level for unit testability) -------------


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get(
                "input_schema", {"type": "object", "properties": {}}
            ),
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
            joined = "\n".join(p for p in text_parts if p).strip()
            msg["content"] = joined or None
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


def _openai_response_to_anthropic(response: Any) -> dict[str, Any]:
    """Normalize the OpenAI SDK's `ChatCompletion` into Anthropic shape.

    The SDK gives us a pydantic-like object; we support `model_dump`,
    `.dict`, and plain dict input so tests can pass dicts directly.
    """
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


# Static-typing safety nets — make sure each concrete class fits the protocol.
_: LLMProvider = OpenAIProvider.__new__(OpenAIProvider)  # type: ignore[abstract]
_: LLMProvider = OpenRouterProvider.__new__(OpenRouterProvider)  # type: ignore[abstract]


__all__ = [
    "OpenAICompatProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "_anthropic_tool_to_openai",
    "_anthropic_to_openai_messages",
    "_openai_response_to_anthropic",
]
