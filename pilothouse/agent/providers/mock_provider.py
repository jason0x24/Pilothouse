"""Mock provider — deterministic replay used when no real LLM is configured.

Walks the `mock_plan` injected into the first user message by the
orchestrator. Each step is either:

    {"tool": "<name>", "input": {...}}   → emit a tool_use turn
    {"final": "<text>"}                  → emit a terminal text turn

When the run has exhausted the plan, returns a generic "all done" final
text. This is what powers keyless local demos + the full test suite.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class MockProvider:
    name = "mock"

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        plan: list[dict[str, Any]] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                try:
                    payload = json.loads(content)
                except Exception:
                    payload = None
                if isinstance(payload, dict) and "mock_plan" in payload:
                    plan = list(payload["mock_plan"])
                    break

        already = 0
        for m in messages:
            if m.get("role") == "assistant":
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        already += 1

        if already < len(plan) and "tool" in plan[already]:
            step = plan[already]
            await asyncio.sleep(0)
            return {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"mock_{already}",
                        "name": step["tool"],
                        "input": step.get("input", {}),
                    }
                ],
                "usage": {"input_tokens": 50, "output_tokens": 30},
            }

        final = "[mock] Plan complete."
        if plan and "final" in plan[-1]:
            final = plan[-1]["final"]
        elif plan and already >= len(plan):
            final = "[mock] All planned tools executed."
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": final}],
            "usage": {"input_tokens": 20, "output_tokens": 60},
        }


__all__ = ["MockProvider"]
