"""LLM provider abstraction.

The agent runtime calls `provider.complete(...)` and expects an
Anthropic-shaped response dict back. That shape was chosen because the
existing runtime + persisted run state already speaks it; non-Anthropic
providers (OpenAI, OpenRouter, ...) translate to/from this shape inside
their own module.

Response contract:

    {
      "stop_reason": "end_turn" | "tool_use" | "max_tokens",
      "content": [
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "<call id>", "name": "<tool>", "input": {...}},
      ],
      "usage": {"input_tokens": int, "output_tokens": int},
    }
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Anything the runtime can hand a turn to.

    Implementations must be stateless w.r.t. one Run (so they can be
    re-instantiated cheaply per request) and async-safe.
    """

    name: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        ...


__all__ = ["LLMProvider"]
