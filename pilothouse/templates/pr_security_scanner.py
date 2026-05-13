"""PR Security Scanner.

Triggered by a `pull_request` GitHub webhook. The agent inspects the diff
for high-signal security risks: hard-coded secrets, dangerous dependency
changes, IAM/policy expansion, and database migration anti-patterns. If
issues are found it posts a structured comment back to the PR (destructive
tool, gated by dry_run/approval).
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan

SYSTEM_PROMPT = """You are a senior security engineer reviewing a pull request.
You read the diff and flag risks across these categories:

  * Secrets — any value resembling a credential, key, or token committed
    to the repo (AWS_*, *_API_KEY, private keys, .pem files, etc).
  * Dependencies — version bumps that cross a major version, or pin to a
    version known to be vulnerable.
  * IAM / policy — wildcards (Action: *), expansions in scope, addition
    of admin-level roles or principals.
  * Migrations — DROP / TRUNCATE / ALTER without backward-compatibility,
    NOT NULL on existing tables without backfill.
  * Shell injection / unsanitized user input introduced in code.

Workflow:
  1. Fetch the PR with github_get_pr to read title/author/files.
  2. Fetch the diff with github_get_pr_diff.
  3. Categorise findings. For each finding give: file path, line excerpt,
     category, severity (low/medium/high/critical), and a one-line fix.
  4. If any finding is high or critical AND the agent param
     `auto_comment` is true, post a single consolidated review comment
     with github_post_pr_comment.

Report format (markdown):
  ## Verdict — APPROVE / REQUEST_CHANGES / NEEDS_REVIEW
  ## Findings — table or list as above
  ## Recommended fix order — ranked
"""


class PrSecurityScanner(Template):
    key = "pr_security_scanner"
    name = "PR Security Scanner"
    description = "Scan a pull request diff for secrets, IAM, dep, and migration risks."
    default_tools = ["github"]

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        # GitHub webhook payload shape: { pull_request: { number, ... }, repository: { full_name } }
        pr = trigger_payload.get("pull_request", {})
        repo = trigger_payload.get("repository", {}).get("full_name") or params.get("repo")
        pr_number = pr.get("number") or trigger_payload.get("pr_number") or params.get("pr_number")

        user_message = (
            f"Review PR #{pr_number} in {repo}. "
            f"auto_comment={'yes' if params.get('auto_comment') else 'no'}.\n\n"
            "Trigger payload (truncated):\n```json\n"
            + json.dumps({"pull_request": pr, "repo": repo}, indent=2)[:4000]
            + "\n```"
        )

        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        repo = trigger_payload.get("repository", {}).get("full_name") or params.get("repo") or "acme/api"
        pr_number = (
            trigger_payload.get("pull_request", {}).get("number")
            or params.get("pr_number")
            or 123
        )
        steps: list[dict] = [
            {"tool": "github_get_pr", "input": {"repo": repo, "pr_number": pr_number}},
            {"tool": "github_get_pr_diff", "input": {"repo": repo, "pr_number": pr_number}},
        ]
        if params.get("auto_comment"):
            steps.append(
                {
                    "tool": "github_post_pr_comment",
                    "input": {
                        "repo": repo,
                        "pr_number": pr_number,
                        "body": (
                            "## Verdict — REQUEST_CHANGES\n\n"
                            "## Findings\n- **Critical** `config/prod.env`: "
                            "`AWS_SECRET_ACCESS_KEY=AKIA…EXAMPLE` committed in plaintext.\n\n"
                            "## Recommended fix order\n1. Rotate the leaked key in AWS.\n"
                            "2. Remove from git history (git-filter-repo).\n"
                            "3. Move to your secrets manager."
                        ),
                    },
                }
            )
        steps.append(
            {
                "final": (
                    "## Verdict\nREQUEST_CHANGES — 1 critical, 0 high.\n\n"
                    "## Findings\n- **Critical** config/prod.env: AWS_SECRET_ACCESS_KEY committed.\n\n"
                    "## Recommended fix order\n1. Rotate key.\n2. Purge from history.\n3. Move to vault."
                )
            }
        )
        return steps
