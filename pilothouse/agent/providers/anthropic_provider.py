"""Anthropic native provider — wraps the official `anthropic` SDK.

Anthropic is the runtime's "home" provider: messages, tools, and the
response shape already match what the rest of Pilothouse uses, so no
translation is needed.

Adding a new native-Anthropic-shaped provider in the future: subclass
or copy this module — almost everything is straight pass-through.
"""

from __future__ import annotations

from typing import Any

from .base import LLMProvider


class AnthropicProvider:
    """Direct Anthropic Claude calls via the `anthropic` SDK."""

    name = "anthropic"

    def __init__(self, *, api_key: str) -> None:
        if not api_key:
            raise ValueError("AnthropicProvider requires an api_key")
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        response = await self._client.messages.create(
            model=model,
            system=system or "",
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
        )
        content: list[dict[str, Any]] = []
        for block in response.content:
            b = block.model_dump() if hasattr(block, "model_dump") else dict(block)
            content.append(b)
        return {
            "stop_reason": response.stop_reason,
            "content": content,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }


# Static check the class fits the protocol.
_: LLMProvider = AnthropicProvider.__new__(AnthropicProvider)  # type: ignore[abstract]


__all__ = ["AnthropicProvider"]
