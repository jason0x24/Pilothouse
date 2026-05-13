"""PR Code Reviewer.

Triggered by a `pull_request` GitHub webhook (`opened` or
`synchronize`). Pulls the PR, the per-file patches, and (optionally)
full file content for files where the patch alone is too thin. Then
posts a single GitHub Review with:

  * a top-level body summarising findings
  * inline comments anchored to specific lines for each issue

Unlike `pr_security_scanner` (which is narrow and pass/fail), this
template is a general code reviewer covering correctness, performance,
readability, security, and tests. The `dimensions` agent param picks
which lenses to apply.
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan

DEFAULT_DIMENSIONS = ["correctness", "performance", "readability", "security", "tests"]

SYSTEM_PROMPT = """You are a senior reviewer doing a careful PR code review.
You file ONE structured review with inline comments — not a flood of
chat-style remarks.

Workflow:
  1. Fetch the PR with github_get_pr.
  2. List files + patches with github_get_pr_files.
  3. For files where the patch context is insufficient (large diffs,
     destructive removals, scope-broadening changes), pull the full file
     with github_get_file_content at the PR's head ref.
  4. Reason across the configured dimensions:
       - correctness: edge cases, null/empty, off-by-one, error paths
       - performance: N+1, accidental quadratic loops, blocking IO
       - readability: dead code, misleading names, comments that lie
       - security: input validation, secrets, IAM, sql/cmd injection
       - tests: coverage gaps for the new behaviour
  5. Submit one review with github_create_pr_review.
       event: APPROVE       → no findings, looks good
       event: COMMENT        → only nits, doesn't block
       event: REQUEST_CHANGES → one or more must-fix findings
  6. Inline comments must reference exact `path` + `line` from the PR
     diff. The line is the line number in the *new* file (RIGHT side).

Rules:
  - Be specific. Vague feedback ("consider refactoring") is not allowed.
  - Quote the line you're commenting on inline, then say what to change.
  - Group nits into one inline comment per file rather than spamming.
  - Never propose changes outside the diff scope unless they're directly
     blocking the PR's correctness.
"""


class PrCodeReviewer(Template):
    key = "pr_code_reviewer"
    name = "PR Code Reviewer"
    description = "Multi-dimensional code review with inline comments on a GitHub PR."
    default_tools = ["github"]

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        pr = trigger_payload.get("pull_request", {})
        repo = trigger_payload.get("repository", {}).get("full_name") or params.get("repo")
        pr_number = pr.get("number") or trigger_payload.get("pr_number") or params.get("pr_number")
        dims = params.get("dimensions") or DEFAULT_DIMENSIONS

        user_message = (
            f"Review PR #{pr_number} in {repo}.\n"
            f"Active dimensions: {', '.join(dims)}.\n"
            f"Block-on-findings (event=REQUEST_CHANGES if any high-severity): "
            f"{params.get('block_on_findings', True)}.\n\n"
            "Trigger payload (truncated):\n```json\n"
            + json.dumps({"pull_request": pr, "repo": repo}, indent=2)[:3000]
            + "\n```"
        )
        return TemplatePlan(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tool_names=params.get("tool_names") or self.default_tools,
        )

    def mock_plan(self, *, trigger_payload: dict, params: dict) -> list[dict]:
        repo = (
            trigger_payload.get("repository", {}).get("full_name")
            or params.get("repo")
            or "acme/api"
        )
        pr_number = (
            trigger_payload.get("pull_request", {}).get("number")
            or params.get("pr_number")
            or 99
        )
        dims = params.get("dimensions") or DEFAULT_DIMENSIONS

        review_body = (
            "## Pilothouse review\n\n"
            f"**Verdict:** REQUEST_CHANGES (1 must-fix, 2 nits)\n\n"
            f"**Dimensions checked:** {', '.join(dims)}\n\n"
            "### Summary\n"
            "Pool size bump is reasonable but the timeout assumption is wrong "
            "(see inline at `pool.py:13`). Test added is weak — only asserts "
            "the constant. Add a behavioural test exercising connection reuse."
        )
        steps = [
            {"tool": "github_get_pr", "input": {"repo": repo, "pr_number": pr_number}},
            {"tool": "github_get_pr_files", "input": {"repo": repo, "pr_number": pr_number}},
            {
                "tool": "github_get_file_content",
                "input": {"repo": repo, "path": "services/checkout/pool.py", "ref": "main"},
            },
            {
                "tool": "github_create_pr_review",
                "input": {
                    "repo": repo,
                    "pr_number": pr_number,
                    "event": "REQUEST_CHANGES",
                    "body": review_body,
                    "comments": [
                        {
                            "path": "services/checkout/pool.py",
                            "line": 13,
                            "side": "RIGHT",
                            "body": (
                                "**Must-fix (correctness).** `POOL_TIMEOUT = 5` "
                                "is in seconds, but the connector calls `wait()` "
                                "with milliseconds (see `db_pool.py:88`). This will "
                                "make pool acquire return after 5ms instead of 5s "
                                "and cause spurious `PoolTimeout`s under load.\n\n"
                                "Suggest: rename to `POOL_TIMEOUT_S = 5` and have "
                                "the call site multiply by 1000."
                            ),
                        },
                        {
                            "path": "tests/test_pool.py",
                            "line": 2,
                            "side": "RIGHT",
                            "body": (
                                "**Nit (tests).** Asserting on the constant just "
                                "tests the assignment statement, not behaviour. "
                                "Add a test that opens the pool and verifies "
                                "connection reuse + timeout actually fires."
                            ),
                        },
                    ],
                },
            },
            {
                "final": (
                    f"## Review submitted\n\n"
                    f"- PR: `{repo}#{pr_number}`\n"
                    f"- Verdict: REQUEST_CHANGES\n"
                    f"- Inline comments: 2\n"
                    f"- Dimensions covered: {', '.join(dims)}"
                )
            },
        ]
        return steps
