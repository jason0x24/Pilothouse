"""Agent templates.

A template is a small Python class that turns a trigger payload (and the
agent's configured params) into the inputs the runtime needs: which tools
to allow, what system prompt, what initial user message. Templates are the
*product* — clients buy them as much as the platform.
"""

from .base import Template, registry
from .bug_auto_fixer import BugAutoFixer
from .datadog_triage import DatadogAlertTriage
from .flaky_test_hunter import FlakyTestHunter
from .k8s_investigator import K8sPodInvestigator
from .pagerduty_first_responder import PagerDutyFirstResponder
from .pr_code_reviewer import PrCodeReviewer
from .pr_security_scanner import PrSecurityScanner
from .terraform_plan_reviewer import TerraformPlanReviewer

__all__ = [
    "Template",
    "registry",
    "BugAutoFixer",
    "DatadogAlertTriage",
    "FlakyTestHunter",
    "K8sPodInvestigator",
    "PagerDutyFirstResponder",
    "PrCodeReviewer",
    "PrSecurityScanner",
    "TerraformPlanReviewer",
]


def register_builtin_templates() -> None:
    """Back-compat shim.

    Built-in templates now load through the plugin manager (each is a
    `builtin.template.<key>` plugin). This function still exists for
    callers/tests that haven't migrated to the manager — it registers
    each built-in directly into the global template registry. Calls
    are idempotent: re-registering the same key just overwrites with
    an equivalent instance.
    """
    for cls in (
        DatadogAlertTriage,
        PrSecurityScanner,
        K8sPodInvestigator,
        TerraformPlanReviewer,
        PagerDutyFirstResponder,
        FlakyTestHunter,
        BugAutoFixer,
        PrCodeReviewer,
    ):
        registry.register(cls())
