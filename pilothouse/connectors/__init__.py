"""Connector framework + built-in connectors.

A *connector* is a named group of *tools*. Tools are what the LLM actually
calls. Each tool declares whether it is destructive (writes external state),
which feeds into Pilothouse's dry-run / approval gates.

In MVP we ship five connectors: Datadog, GitHub, PagerDuty, Slack and
Kubernetes. When no API key is configured a connector runs in *mock mode*:
it returns synthetic but structurally realistic data so templates can be
developed end-to-end without external credentials.
"""

from .base import Connector, Tool, ToolContext, ToolResult, registry
from .datadog import DatadogConnector
from .github import GitHubConnector
from .kubernetes import KubernetesConnector
from .linear import LinearConnector
from .pagerduty import PagerDutyConnector
from .slack import SlackConnector

__all__ = [
    "Connector",
    "Tool",
    "ToolContext",
    "ToolResult",
    "registry",
    "DatadogConnector",
    "GitHubConnector",
    "KubernetesConnector",
    "LinearConnector",
    "PagerDutyConnector",
    "SlackConnector",
]


def register_builtin_connectors() -> None:
    """Back-compat shim — see `pilothouse.plugins.builtin` for the real path.

    Built-in connectors now load through the plugin manager (each is a
    `builtin.<name>` plugin). This function still exists for callers
    that haven't migrated. Re-registering is idempotent.
    """
    for cls in (
        DatadogConnector,
        GitHubConnector,
        PagerDutyConnector,
        SlackConnector,
        KubernetesConnector,
        LinearConnector,
    ):
        registry.register(cls())
