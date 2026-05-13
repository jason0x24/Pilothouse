"""PagerDuty First Responder.

Triggered by a PagerDuty webhook the moment a high-urgency incident
fires. The agent pulls the incident details, fetches a service-level
metric from Datadog for context, posts a structured "I'm on it" update
to Slack so the on-call team has shared situational awareness, and adds
a note to the incident with what's been gathered. It does NOT ack — ack
is a human decision; the agent prepares the ground.
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan

SYSTEM_PROMPT = """You are the first responder agent for a paging tier.
When a PagerDuty incident fires, you have ~60 seconds to gather context
the on-call engineer would otherwise grab manually:

  1. Fetch the incident with pagerduty_get_incident.
  2. Identify the service from the incident summary/service field.
  3. Pull a service-level metric for the last 30 minutes via
     datadog_query_metric to confirm the breach is real.
  4. Search logs in the same window for errors hinting at the cause.
  5. Post a "context bundle" to the on-call Slack channel via
     slack_post_message — link the incident, summarise the breach,
     include one or two log/metric data points.
  6. Add the same summary as a note on the incident via pagerduty_add_note.

You do NOT acknowledge the incident. Acks are for humans.

Slack message structure:
  *PD#<num>* `<service>` — <one-line breach summary>
  ↳ metric: <value>; logs: <one signal>; runbook: <runbook param or "n/a">

Note structure (same content, multi-line):
  - Time the breach was first observed
  - Top error log message (verbatim, max 200 chars)
  - Suggested next step
"""


class PagerDutyFirstResponder(Template):
    key = "pagerduty_first_responder"
    name = "PagerDuty First Responder"
    description = "Gather context for a fresh PagerDuty incident and post it to Slack + the incident note."
    default_tools = ["pagerduty", "datadog", "slack"]

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        # PagerDuty v3 webhook shape: messages[0].incident.id
        incident_id = (
            params.get("incident_id_override")
            or trigger_payload.get("incident", {}).get("id")
            or _extract_pd_incident_id(trigger_payload)
            or trigger_payload.get("incident_id")
            or "unknown"
        )
        slack_channel = params.get("slack_channel") or "#oncall"
        runbook = params.get("runbook") or "n/a"

        user_message = (
            f"Triage PagerDuty incident {incident_id}. "
            f"Slack channel: {slack_channel}. Runbook: {runbook}.\n\n"
            "Trigger payload (truncated):\n```json\n"
            + json.dumps(trigger_payload, indent=2)[:3000]
            + "\n```"
        )
        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        incident_id = (
            params.get("incident_id_override")
            or trigger_payload.get("incident", {}).get("id")
            or _extract_pd_incident_id(trigger_payload)
            or trigger_payload.get("incident_id")
            or "PINC12345"
        )
        service = params.get("service", "checkout")
        channel = params.get("slack_channel", "#oncall")
        steps: list[dict] = [
            {"tool": "pagerduty_get_incident", "input": {"incident_id": incident_id}},
            {
                "tool": "datadog_query_metric",
                "input": {
                    "query": f"avg:trace.web.request.duration.95p{{service:{service}}}",
                    "from_minutes_ago": 30,
                },
            },
            {
                "tool": "datadog_search_logs",
                "input": {
                    "query": f"service:{service} status:error",
                    "from_minutes_ago": 30,
                },
            },
            {
                "tool": "slack_post_message",
                "input": {
                    "channel": channel,
                    "text": (
                        f"*PD#{incident_id}* `{service}` — p95 latency breach.\n"
                        "↳ metric p95≈520ms (SLO 800ms still hot); "
                        "logs: `ConnectionPoolTimeout: pool size 20 exhausted`; "
                        f"runbook: {params.get('runbook', 'n/a')}"
                    ),
                },
            },
            {
                "tool": "pagerduty_add_note",
                "input": {
                    "incident_id": incident_id,
                    "content": (
                        f"First-responder context for {service}:\n"
                        "- p95 latency began climbing 12 minutes ago, peaked ~520ms\n"
                        "- Top log: `ConnectionPoolTimeout: pool size 20 exhausted`\n"
                        "- Suggested next step: check the most recent deploy and roll back "
                        "if it changed the connection pool size."
                    ),
                },
            },
            {
                "final": (
                    f"Context bundle posted to {channel} and appended as a note on "
                    f"PD#{incident_id}. Did not acknowledge — leaving that decision "
                    "to the on-call engineer."
                )
            },
        ]
        return steps


def _extract_pd_incident_id(payload: dict) -> str | None:
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        inc = msgs[0].get("incident")
        if isinstance(inc, dict):
            return inc.get("id")
    return None
