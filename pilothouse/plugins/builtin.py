"""Built-in plugins — the templates and connectors that ship with the wheel.

These look exactly like third-party plugins from the manager's view —
the same registration path, the same enable/disable knobs. An operator
who only wants the bug-fix workflow can `pilothouse plugins disable
builtin.datadog` and the Datadog connector + templates that need it
won't be available.

Built-ins are grouped by *concern*, not packed into one giant plugin,
so they can be disabled individually. The grouping mirrors the original
file layout in pilothouse/templates and pilothouse/connectors.
"""

from __future__ import annotations

from ..connectors.base import Connector
from ..templates.base import Template
from .base import ConnectorPlugin, PluginMeta, TemplatePlugin


# --- helpers ------------------------------------------------------------


class _SingleTemplate(TemplatePlugin):
    """Adapter: one Template ↔ one plugin row. Operators can disable
    individual templates without affecting the rest."""

    def __init__(self, name: str, template_cls: type[Template], description: str) -> None:
        self.name = name
        self._template_cls = template_cls
        self._description = description

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description=self._description,
            kinds=set(self._inferred_kinds()),
        )

    def templates(self) -> list[Template]:
        return [self._template_cls()]


class _SingleConnector(ConnectorPlugin):
    """Adapter: one Connector ↔ one plugin row."""

    def __init__(self, name: str, connector_cls: type[Connector], description: str) -> None:
        self.name = name
        self._connector_cls = connector_cls
        self._description = description

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name,
            version="0.1.0",
            description=self._description,
            kinds=set(self._inferred_kinds()),
        )

    def connectors(self) -> list[Connector]:
        return [self._connector_cls()]


# --- registry --------------------------------------------------------


def builtin_plugins() -> list:
    """Eager import + instantiate. Cheap because everything's in the
    same wheel."""
    from ..connectors.datadog import DatadogConnector
    from ..connectors.github import GitHubConnector
    from ..connectors.kubernetes import KubernetesConnector
    from ..connectors.linear import LinearConnector
    from ..connectors.pagerduty import PagerDutyConnector
    from ..connectors.slack import SlackConnector
    from ..templates.bug_auto_fixer import BugAutoFixer
    from ..templates.datadog_triage import DatadogAlertTriage
    from ..templates.flaky_test_hunter import FlakyTestHunter
    from ..templates.k8s_investigator import K8sPodInvestigator
    from ..templates.pagerduty_first_responder import PagerDutyFirstResponder
    from ..templates.pr_code_reviewer import PrCodeReviewer
    from ..templates.pr_security_scanner import PrSecurityScanner
    from ..templates.terraform_plan_reviewer import TerraformPlanReviewer

    connectors: list = [
        _SingleConnector("builtin.datadog", DatadogConnector, "Datadog metric / alert / log connector"),
        _SingleConnector("builtin.github", GitHubConnector, "GitHub repo / PR / file / review connector"),
        _SingleConnector("builtin.pagerduty", PagerDutyConnector, "PagerDuty incident connector"),
        _SingleConnector("builtin.slack", SlackConnector, "Slack message connector"),
        _SingleConnector("builtin.kubernetes", KubernetesConnector, "Kubernetes API connector"),
        _SingleConnector("builtin.linear", LinearConnector, "Linear issue tracker connector"),
    ]
    templates: list = [
        _SingleTemplate(
            "builtin.template.datadog_alert_triage",
            DatadogAlertTriage,
            "Investigate a firing Datadog alert and produce a triage report.",
        ),
        _SingleTemplate(
            "builtin.template.pr_security_scanner",
            PrSecurityScanner,
            "Scan a PR diff for secrets, IAM, dep, and migration risks.",
        ),
        _SingleTemplate(
            "builtin.template.k8s_pod_investigator",
            K8sPodInvestigator,
            "Diagnose a CrashLoopBackOff / Failed pod and rank causes.",
        ),
        _SingleTemplate(
            "builtin.template.terraform_plan_reviewer",
            TerraformPlanReviewer,
            "Risk-tier a Terraform plan and post a structured review.",
        ),
        _SingleTemplate(
            "builtin.template.pagerduty_first_responder",
            PagerDutyFirstResponder,
            "Gather context for a fresh PagerDuty incident; post to Slack + incident note.",
        ),
        _SingleTemplate(
            "builtin.template.flaky_test_hunter",
            FlakyTestHunter,
            "Nightly scan that surfaces intermittently-failing tests.",
        ),
        _SingleTemplate(
            "builtin.template.bug_auto_fixer",
            BugAutoFixer,
            "Pick up a tagged Linear bug, write a small fix, open a PR.",
        ),
        _SingleTemplate(
            "builtin.template.pr_code_reviewer",
            PrCodeReviewer,
            "Multi-dimensional code review with inline comments.",
        ),
    ]
    return connectors + templates
