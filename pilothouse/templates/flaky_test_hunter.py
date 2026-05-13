"""Flaky Test Hunter.

Cron-driven (nightly). Scans recent commits + check runs on a repo,
finds tests that fail intermittently across runs of the same commit /
adjacent commits, and posts a digest comment on a tracking issue.

For MVP the connector surface is just GitHub — we lean on
`github_list_recent_commits` plus a synthesised flaky-test signal in
mock mode. A real install would extend the github connector with
`list_check_runs` + `get_check_run_logs` to parse junit-style output.
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan

SYSTEM_PROMPT = """You are a quality engineer hunting flaky tests.

Cadence: you run nightly. For each repo configured you:
  1. Pull the latest N commits with github_list_recent_commits.
  2. (Future) Inspect each commit's CI check runs for tests that
     transitioned pass→fail→pass without code change. In MVP, you read
     the synthetic flaky signal we provide and produce a digest.
  3. Rank tests by flake frequency (failures per N runs).
  4. If the agent param `auto_comment` is set and a `tracking_issue`
     number is provided, post a digest comment on that issue with
     github_post_pr_comment (issue comments use the same endpoint).

Digest format:
  ## Flaky tests — last <window>
  | rank | test | failures / runs | last seen |
  | --- | --- | --- | --- |
  ...
  ## Suggested actions
"""


class FlakyTestHunter(Template):
    key = "flaky_test_hunter"
    name = "Flaky Test Hunter"
    description = "Nightly scan that surfaces intermittently-failing tests across recent commits."
    default_tools = ["github"]
    suggested_schedule = "0 5 * * *"  # 05:00 UTC nightly

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        repo = params.get("repo") or trigger_payload.get("repo") or "acme/api"
        window = params.get("window") or "last 24h"
        tracking_issue = params.get("tracking_issue")
        user_message = (
            f"Hunt flaky tests in {repo} over {window}. "
            f"auto_comment={'yes' if params.get('auto_comment') else 'no'}, "
            f"tracking_issue={tracking_issue or 'none'}.\n\n"
            "Trigger payload:\n```json\n"
            + json.dumps(trigger_payload, indent=2)[:1500]
            + "\n```"
        )
        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        repo = params.get("repo") or "acme/api"
        tracking_issue = params.get("tracking_issue") or 1
        steps: list[dict] = [
            {"tool": "github_list_recent_commits", "input": {"repo": repo, "limit": 20}},
        ]
        digest_body = (
            "## Flaky tests — last 24h\n"
            "| rank | test | failures / runs | last seen |\n"
            "| --- | --- | --- | --- |\n"
            "| 1 | tests/payments/test_retry.py::test_circuit_breaker | 7 / 23 | 38m ago |\n"
            "| 2 | tests/orders/test_split.py::test_partial_refund | 4 / 23 | 2h ago |\n"
            "| 3 | tests/auth/test_session.py::test_token_refresh_race | 3 / 23 | 5h ago |\n\n"
            "## Suggested actions\n"
            "- Quarantine #1 behind `pytest.mark.flaky(reruns=3)` until owned.\n"
            "- #2 looks timing-sensitive — check for `time.sleep` ↔ asyncio interactions.\n"
            "- #3 races a token-refresh background task; consider an event-driven test."
        )
        if params.get("auto_comment"):
            steps.append(
                {
                    "tool": "github_post_pr_comment",
                    "input": {
                        "repo": repo,
                        "pr_number": tracking_issue,
                        "body": digest_body,
                    },
                }
            )
        steps.append({"final": digest_body})
        return steps
