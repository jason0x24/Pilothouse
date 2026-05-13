"""Template base class + registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TemplatePlan:
    system_prompt: str
    user_message: str
    tool_names: list[str]  # connector or tool names; runtime resolves via registry


class Template:
    """A reusable agent definition.

    Subclasses override `plan()` to produce a TemplatePlan from the runtime
    trigger payload + agent params. They may also override `mock_plan()` to
    define what the no-key replay should do — useful for demos.
    """

    key: str = ""
    name: str = ""
    description: str = ""
    # default tool names exposed to this template
    default_tools: list[str] = []
    # cron string suggested at creation time (operators can override)
    suggested_schedule: str | None = None

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        raise NotImplementedError

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        """Sequence of tool calls + final text used when no API key is set."""
        return [{"final": "[mock] no plan defined"}]


class _Registry:
    def __init__(self) -> None:
        self.templates: dict[str, Template] = {}

    def register(self, template: Template) -> None:
        self.templates[template.key] = template

    def get(self, key: str) -> Template:
        if key not in self.templates:
            raise KeyError(f"unknown template: {key}")
        return self.templates[key]

    def all(self) -> list[Template]:
        return list(self.templates.values())


registry = _Registry()
