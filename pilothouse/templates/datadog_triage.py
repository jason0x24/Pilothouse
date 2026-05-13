"""Datadog Alert Triage — the flagship template.

Triggered by a Datadog webhook (or PagerDuty incident with an embedded
Datadog alert ID). The agent:

  1. Pulls the alert definition + recent metric values.
  2. Lists recent deploys for the affected service.
  3. Searches logs for related errors in the alert window.
  4. Synthesises a triage report: likely cause, blast radius, suggested
     next action, candidate runbook step.
  5. Optionally posts the summary to Slack and appends a note to the
     PagerDuty incident.

The system prompt is deliberately opinionated — it bakes in the structure
we want every triage report to follow, so downstream consumers (Slack
recipients, on-call) get a consistent format.
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan

SYSTEM_PROMPT = """You are an SRE triage agent. You are paged when a Datadog alert fires.
Your job is to gather context fast and produce a tight, actionable report.

Workflow:
  1. Fetch the alert with datadog_get_alert.
  2. Identify the affected service from the alert tags. Pull its metric
     timeseries for the last 60 minutes with datadog_query_metric.
  3. List recent deploys to that service with datadog_recent_deploys.
  4. Search logs for ERROR-level entries in the alert window using a
     focused query (service:<svc> status:error).
  5. (If a slack_channel param is set) post a concise summary to Slack.
  6. (If a pagerduty_incident_id is in the trigger payload) add the same
     summary as a note on the incident.

Report format (use markdown headings):
  ## Summary — one sentence, includes service and SLO breach magnitude
  ## Likely cause — top 1-2 hypotheses, ranked
  ## Evidence — bullet list of concrete observations from tool calls
  ## Recent deploys — relevant deploys with version + minutes ago
  ## Suggested next action — single concrete next step for the on-call
  ## Confidence — high / medium / low + one sentence of why

Rules:
  - Do not speculate beyond what tool results support.
  - Prefer specific values (latencies, version numbers) over hand-waving.
  - If you cannot determine the affected service, say so plainly and stop.
"""


class DatadogAlertTriage(Template):
    key = "datadog_alert_triage"
    name = "Datadog Alert Triage"
    description = "Investigate a firing Datadog alert and produce a triage report."
    default_tools = ["datadog", "slack", "pagerduty"]

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        alert_id = (
            trigger_payload.get("alert_id")
            or trigger_payload.get("id")
            or params.get("alert_id")
            or "unknown"
        )
        incident_id = trigger_payload.get("pagerduty_incident_id")
        slack_channel = params.get("slack_channel")

        user_message = (
            f"Triage Datadog alert {alert_id}. "
            + (f"PagerDuty incident: {incident_id}. " if incident_id else "")
            + (f"Post the summary to Slack channel {slack_channel}. " if slack_channel else "")
            + "\n\nTrigger payload:\n```json\n"
            + json.dumps(trigger_payload, indent=2)
            + "\n```"
        )

        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        alert_id = trigger_payload.get("alert_id", "12345")
        service = params.get("service", "checkout")
        steps: list[dict] = [
            {"tool": "datadog_get_alert", "input": {"alert_id": alert_id}},
            {
                "tool": "datadog_query_metric",
                "input": {
                    "query": f"avg:trace.web.request.duration.95p{{service:{service}}}",
                    "from_minutes_ago": 60,
                },
            },
            {"tool": "datadog_recent_deploys", "input": {"service": service}},
            {
                "tool": "datadog_search_logs",
                "input": {"query": f"service:{service} status:error", "from_minutes_ago": 30},
            },
        ]
        if params.get("slack_channel"):
            steps.append(
                {
                    "tool": "slack_post_message",
                    "input": {
                        "channel": params["slack_channel"],
                        "text": f"[Pilothouse triage] {service} latency SLO breach — see thread.",
                    },
                }
            )
        steps.append(
            {
                "final": (
                    f"## Summary\n{service} p95 latency exceeded 800ms SLO; impact moderate.\n\n"
                    f"## Likely cause\n1. Recent deploy 12 minutes ago introduced regression.\n"
                    f"2. Connection pool saturation under elevated load.\n\n"
                    f"## Evidence\n- p95 climbed from ~150ms to ~520ms at t+40 in the window.\n"
                    f"- Logs show repeated 'ConnectionPoolTimeout: pool size 20 exhausted'.\n"
                    f"- Deploy of {service} v17.4.x landed 12 minutes before alert.\n\n"
                    f"## Suggested next action\nRoll back the latest {service} deploy and rerun.\n\n"
                    f"## Confidence\nHigh — deploy timing and pool-exhaustion logs align."
                )
            }
        )
        return steps
