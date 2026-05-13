"""Tool/connector primitives.

Design notes:

- Each Tool is a typed, awaitable function. We *don't* try to auto-generate
  schemas from Python annotations — JSON Schema is what Anthropic's API
  consumes, so authors write it explicitly. This keeps the contract honest.
- `is_destructive` flips two safeties: in dry-run the runtime returns a
  synthesized "would have done X" result without invoking the handler, and
  with `require_approval_for_writes` the runtime parks the run in
  `awaiting_approval` until a human resolves the Approval row.
- `ToolContext` is what the runtime injects when invoking a tool — handlers
  use it to read the current run/agent, to log structured events, and to
  reach the global settings without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ToolResult:
    """Returned by a tool handler. `content` is what the LLM sees."""

    content: str | dict | list
    is_error: bool = False
    # Free-form metadata persisted alongside the tool_result event. Useful for
    # later audit (e.g. external request IDs, sanitized response excerpts).
    metadata: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    run_id: str
    agent_id: str
    dry_run: bool
    params: dict
    # Lazy emitter for structured events. Templates / tools can attach
    # observations without touching the DB session directly.
    emit: Callable[[str, dict], Awaitable[None]]


ToolHandler = Callable[[ToolContext, dict], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: ToolHandler
    is_destructive: bool = False
    connector: str = ""

    def to_anthropic(self) -> dict:
        """Schema in the shape Anthropic's `tools` parameter expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class Connector:
    """Base class. Subclasses populate `_tools` in `__init__`."""

    name: str = ""

    def __init__(self) -> None:
        self._tools: list[Tool] = []

    def tools(self) -> list[Tool]:
        return list(self._tools)

    @property
    def live(self) -> bool:
        """True when we have real credentials; otherwise we run in mock mode."""
        return False

    def _add(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: ToolHandler,
        *,
        is_destructive: bool = False,
    ) -> None:
        self._tools.append(
            Tool(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=handler,
                is_destructive=is_destructive,
                connector=self.name,
            )
        )


class _Registry:
    """Process-wide registry of connectors and a flat tool lookup."""

    def __init__(self) -> None:
        self.connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        self.connectors[connector.name] = connector

    def all_tools(self) -> dict[str, Tool]:
        out: dict[str, Tool] = {}
        for c in self.connectors.values():
            for t in c.tools():
                out[t.name] = t
        return out

    def tools_for(self, names: list[str]) -> list[Tool]:
        """Filter tools by connector name or explicit tool name."""
        if not names:
            return list(self.all_tools().values())
        out: list[Tool] = []
        seen: set[str] = set()
        flat = self.all_tools()
        for n in names:
            if n in self.connectors:
                for t in self.connectors[n].tools():
                    if t.name not in seen:
                        out.append(t)
                        seen.add(t.name)
            elif n in flat and n not in seen:
                out.append(flat[n])
                seen.add(n)
        return out


registry = _Registry()


def as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    import json

    return json.dumps(content, ensure_ascii=False, indent=2, default=str)
