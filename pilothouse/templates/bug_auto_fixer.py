"""Bug auto-fixer.

Trigger:
  * **Cron** — poll Linear for issues labelled `pilothouse-fix` (default)
    in `Triage` state and pick one.
  * **Linear webhook** — issue created/updated with the trigger label.

Workflow:
  1. linear_get_issue — read the bug report.
  2. github_get_file_content — pull the file the bug points at (the
     issue body should reference a path; in mock mode we use the
     synthesised path from `_mock_issue`).
  3. Generate a minimal fix.
  4. github_create_branch — branch from main using the agreed convention
     (`pilothouse/fix/<TICKET-ID>-<slug>`).
  5. github_create_or_update_file — commit with a Conventional Commits
     message that closes the ticket.
  6. github_create_pull_request — open the PR.
  7. linear_add_comment — link the PR back to the issue.

The destructive steps (4-7) are subject to dry-run + approval gates as
usual; in dry-run the agent walks the full plan, prints what it *would*
do, and never touches the remote.
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan
from ..git_conventions import render_branch_spec, slugify

SYSTEM_PROMPT = """You are an auto-fix engineer. Given one bug ticket
from a tracker, you write the smallest, safest patch that resolves it
and open a pull request through Pilothouse's git tooling.

Your strict workflow:
  1. Fetch the ticket with linear_get_issue.
  2. Identify the file path referenced in the description. Read it with
     github_get_file_content.
  3. Reason about the minimum viable patch. Prefer:
       * Returning early on the failure mode (None checks, len() == 0)
       * Adding a regression test in the same PR if the harness allows
       * NOT refactoring unrelated code in the same PR
  4. Compose the new file content (full content, not a diff — the tool
     uploads the whole file).
  5. Create a branch named `<branch_prefix>/fix/<TICKET-ID>-<slug>`.
     Use the *exact* slug returned by the helper — don't invent your own.
  6. Commit with a Conventional Commits subject: `fix(<scope>): …`
     where `<scope>` is the module name (e.g. `users`, `payments`).
  7. Open a PR. Title = commit subject. Body = problem + fix + files +
     test note + `Closes <TICKET-ID>`.
  8. Add a Linear comment linking the new PR.

Rules:
  - Never modify files the ticket doesn't implicate.
  - Never delete tests.
  - If the bug is unclear, comment on Linear asking for repro and
     STOP — do NOT open a speculative PR.
  - All destructive ops must be approved (dry-run shows you what they
     would do without executing).
"""


class BugAutoFixer(Template):
    key = "bug_auto_fixer"
    name = "Bug Auto-Fixer"
    description = "Pick up a tagged Linear bug, write the smallest safe fix, open a PR."
    default_tools = ["linear", "github"]
    suggested_schedule = "*/15 * * * *"  # poll every 15 min

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        # Single-ticket mode: a webhook payload includes the issue id;
        # cron mode: the agent first calls linear_list_issues itself.
        ticket_id = (
            params.get("ticket_id_override")
            or trigger_payload.get("issue", {}).get("identifier")
            or trigger_payload.get("ticket_id")
        )
        repo = params.get("repo") or "acme/api"
        label = params.get("label") or "pilothouse-fix"
        team = params.get("team_key") or "ENG"

        user_message = (
            (
                f"Single ticket mode: fix Linear issue {ticket_id} in repo {repo}."
                if ticket_id
                else (
                    f"Cron mode: list issues in team {team} with label '{label}', "
                    "pick the highest-priority one not already in progress, then fix it "
                    f"in {repo}. If nothing matches, do nothing and explain why."
                )
            )
            + f"\n\nBranch prefix: `{params.get('branch_prefix', 'pilothouse')}` (overrideable).\n"
            + "Commit message must be Conventional Commits and include `Closes <TICKET-ID>`.\n\n"
            + "Trigger payload:\n```json\n"
            + json.dumps(trigger_payload, indent=2)[:2000]
            + "\n```"
        )
        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        ticket_id = (
            params.get("ticket_id_override")
            or trigger_payload.get("issue", {}).get("identifier")
            or trigger_payload.get("ticket_id")
            or "ENG-1234"
        )
        repo = params.get("repo") or "acme/api"
        label = params.get("label") or "pilothouse-fix"
        prefix = params.get("branch_prefix") or "pilothouse"

        # Compute the canonical branch/commit/PR strings via the helper —
        # the same code the live model is told to follow. The mock plan
        # is therefore a deterministic example of "the right answer".
        spec = render_branch_spec(
            ticket_id=ticket_id,
            title="NPE in get_user when DB lookup misses",
            summary=(
                "Customers hitting `/users/<id>` with a non-existent id received a "
                "500 because `get_user` did `DB[user_id].copy()` on `None`. "
                "This change returns `None` early; the API layer already maps "
                "`None` → 404, so callers see the right status."
            ),
            commit_type="fix",
            scope="users",
            body="Adds an early return in `get_user` and a regression test.",
            files=["services/users/api.py", "tests/users/test_get_user.py"],
            test_note="Added `test_get_user_returns_none_for_missing_id` — passes locally.",
            agent_name="bug_auto_fixer",
            branch_prefix=prefix,
        )

        new_file_content = (
            "def get_user(user_id):\n"
            "    record = DB.get(user_id)\n"
            "    if record is None:\n"
            "        return None\n"
            "    return record.copy()\n"
            "\n"
            "def list_users():\n"
            "    return [u for u in DB.values()]\n"
        )

        steps: list[dict] = []
        if not (
            params.get("ticket_id_override")
            or trigger_payload.get("issue", {}).get("identifier")
            or trigger_payload.get("ticket_id")
        ):
            # Cron mode kicks off with a list call.
            steps.append(
                {
                    "tool": "linear_list_issues",
                    "input": {"team_key": params.get("team_key", "ENG"), "label": label, "limit": 5},
                }
            )
        steps += [
            {"tool": "linear_get_issue", "input": {"issue_id": ticket_id}},
            {
                "tool": "github_get_file_content",
                "input": {"repo": repo, "path": "services/users/api.py", "ref": "main"},
            },
            {
                "tool": "github_create_branch",
                "input": {"repo": repo, "branch": spec.branch, "from_ref": "main"},
            },
            {
                "tool": "github_create_or_update_file",
                "input": {
                    "repo": repo,
                    "branch": spec.branch,
                    "path": "services/users/api.py",
                    "content": new_file_content,
                    "message": spec.commit_message,
                },
            },
            {
                "tool": "github_create_pull_request",
                "input": {
                    "repo": repo,
                    "head": spec.branch,
                    "base": "main",
                    "title": spec.pr_title,
                    "body": spec.pr_body,
                    "draft": False,
                },
            },
            {
                "tool": "linear_add_comment",
                "input": {
                    "issue_id": ticket_id,
                    "content": (
                        f"Pilothouse opened a PR with a candidate fix on branch "
                        f"`{spec.branch}`. Review and merge when ready."
                    ),
                },
            },
            {
                "final": (
                    f"## Auto-fix opened\n\n"
                    f"- Ticket: **{ticket_id}**\n"
                    f"- Repo: `{repo}`\n"
                    f"- Branch: `{spec.branch}`\n"
                    f"- Commit subject: `{spec.pr_title}`\n"
                    f"- Files changed: `services/users/api.py`\n\n"
                    f"PR is open and the Linear issue has a comment linking back."
                )
            },
        ]
        return steps
