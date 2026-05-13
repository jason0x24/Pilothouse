"""K8s Pod Crash Investigator.

Triggered by an Alertmanager webhook with labels {pod, namespace}. Uses
the Kubernetes connector for the authoritative pod state (describe,
events, logs) and Datadog for cross-cutting context (deploy history,
service-level metrics). Outputs the five most plausible failure causes
ranked by evidence weight.
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan

SYSTEM_PROMPT = """You are an SRE who diagnoses Kubernetes pod failures.
You receive an Alertmanager alert and produce a ranked list of probable
causes with evidence pointers.

Workflow (in order):
  1. From the alert payload extract namespace + pod name.
  2. Describe the pod with kubernetes_describe_pod to read container
     statuses, restart counts, last terminated state.
  3. Pull recent events with kubernetes_get_pod_events.
  4. Tail logs with kubernetes_get_pod_logs (set previous=true if the
     current container is not yet running).
  5. If a service label is available, pull recent deploys for context
     with datadog_recent_deploys.
  6. Produce a ranked list of 5 candidate causes. For each include:
     - cause (one line)
     - severity of impact
     - evidence (specific log line / event reason / container state)
     - suggested action

Rules:
  - Anchor every claim to a specific tool result. No vague guesses.
  - If `lastState.terminated.reason` is OOMKilled, that is overwhelming
    evidence — rank it first.
  - If logs show `ImagePullBackOff`, the cluster cannot fetch the image
    — usually a registry credential or tag issue.

Report format:
  ## Pod
  ## Top 5 candidate causes (ranked)
  ## Highest-confidence next step
"""


class K8sPodInvestigator(Template):
    key = "k8s_pod_investigator"
    name = "K8s Pod Crash Investigator"
    description = "Diagnose a CrashLoopBackOff / Failed pod and rank likely causes."
    default_tools = ["kubernetes", "datadog"]

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        labels = trigger_payload.get("commonLabels") or trigger_payload.get("labels") or {}
        pod = labels.get("pod") or params.get("pod") or "unknown"
        namespace = labels.get("namespace") or params.get("namespace") or "default"

        user_message = (
            f"Investigate pod {namespace}/{pod}.\n\n"
            "Alertmanager payload:\n```json\n"
            + json.dumps(trigger_payload, indent=2)[:3000]
            + "\n```"
        )
        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        labels = trigger_payload.get("commonLabels") or trigger_payload.get("labels") or {}
        pod = labels.get("pod") or params.get("pod") or "checkout-7d8c-xyz"
        ns = labels.get("namespace") or params.get("namespace") or "default"
        service = labels.get("service") or params.get("service") or "checkout"
        return [
            {"tool": "kubernetes_describe_pod", "input": {"namespace": ns, "name": pod}},
            {"tool": "kubernetes_get_pod_events", "input": {"namespace": ns, "name": pod}},
            {
                "tool": "kubernetes_get_pod_logs",
                "input": {"namespace": ns, "name": pod, "tail_lines": 100, "previous": True},
            },
            {"tool": "datadog_recent_deploys", "input": {"service": service}},
            {
                "final": (
                    f"## Pod\n{ns}/{pod}\n\n"
                    "## Top 5 candidate causes (ranked)\n"
                    "1. **OOMKilled** — `lastState.terminated.reason=OOMKilled` and "
                    "memory limit only 256Mi. Severity: high.\n"
                    "2. **Failed liveness probe** — events show repeated "
                    "`Unhealthy: Liveness probe failed: HTTP 500`. Severity: high.\n"
                    "3. **Image rolled in last deploy is bad** — deploy v17.4.x landed "
                    "12 minutes before alert; restartCount jumped after.\n"
                    "4. **ConfigMap/Secret missing key** — log line "
                    "`failed to open /etc/secrets/api_key` after recent rename.\n"
                    "5. **Node-level memory pressure** — events include "
                    "`FailedScheduling: insufficient memory`.\n\n"
                    "## Highest-confidence next step\n"
                    "Raise container memory request/limit (256Mi → 1Gi) and re-roll "
                    f"the {service} deployment. If OOMKilled persists, profile heap "
                    "on the latest version before reverting."
                )
            },
        ]
