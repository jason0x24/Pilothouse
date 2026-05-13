"""Terraform Plan Reviewer.

Triggered by a GitHub PR webhook. Assumes the PR description or an
attached comment contains the `terraform plan` output (Atlantis convention).
The agent reads the diff + the plan, classifies each change in the plan
into safe / risky / blocking, estimates blast-radius (e.g. destroy of a
DB), and posts a structured review comment.

This template intentionally avoids a dedicated Terraform connector — in
practice plan output lives in PR comments or check runs, so we get more
mileage out of the existing `github` connector.
"""

from __future__ import annotations

import json

from .base import Template, TemplatePlan

SYSTEM_PROMPT = """You are a senior infrastructure engineer reviewing a Terraform PR.
You weigh the diff and the plan output, classify each change, and produce
a comment the PR author can act on without further back-and-forth.

Classification:
  * BLOCKING — DROP / DELETE of stateful resources (databases, S3 buckets
    with data, KMS keys), IAM principal expansions, security-group rules
    opening 0.0.0.0/0 on sensitive ports.
  * RISKY — replacements that imply downtime (forced replace), changes
    to provider versions, large reductions in `count` / `for_each`.
  * SAFE — additive resource creation, tag changes, output-only changes.

Workflow:
  1. github_get_pr — read title, author, base/head.
  2. github_get_pr_diff — read the code diff.
  3. github_list_recent_commits — sanity-check the branch.
  4. Build a classification per resource touched in the plan, deriving
     the plan from the diff when an explicit plan block is absent.
  5. If `auto_comment` is set, post the review with github_post_pr_comment.

Report format:
  ## Verdict — APPROVE / REQUEST_CHANGES / BLOCK
  ## Changes by class
    - **BLOCKING** — resource, action, why
    - **RISKY** — resource, action, why
    - **SAFE** — short summary
  ## Suggested apply order
  ## Suggested follow-ups
"""


class TerraformPlanReviewer(Template):
    key = "terraform_plan_reviewer"
    name = "Terraform Plan Reviewer"
    description = "Classify Terraform plan changes and post a risk-tiered review on the PR."
    default_tools = ["github"]

    def plan(self, *, trigger_payload: dict, params: dict) -> TemplatePlan:
        pr = trigger_payload.get("pull_request", {})
        repo = trigger_payload.get("repository", {}).get("full_name") or params.get("repo")
        pr_number = pr.get("number") or trigger_payload.get("pr_number") or params.get("pr_number")

        user_message = (
            f"Review the Terraform PR #{pr_number} in {repo}. "
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
        repo = (
            trigger_payload.get("repository", {}).get("full_name")
            or params.get("repo")
            or "acme/infra"
        )
        pr_number = (
            trigger_payload.get("pull_request", {}).get("number")
            or params.get("pr_number")
            or 88
        )
        steps: list[dict] = [
            {"tool": "github_get_pr", "input": {"repo": repo, "pr_number": pr_number}},
            {"tool": "github_get_pr_diff", "input": {"repo": repo, "pr_number": pr_number}},
            {"tool": "github_list_recent_commits", "input": {"repo": repo, "limit": 5}},
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
                            "## Changes by class\n"
                            "- **BLOCKING** `aws_rds_cluster.primary` — destroy. "
                            "Stateful resource; will lose data unless `prevent_destroy`.\n"
                            "- **RISKY** `aws_security_group.public` — adds ingress 0.0.0.0/0:22. "
                            "Bastion or port 22 to the world is almost always wrong.\n"
                            "- **SAFE** 4 additive resources (3 IAM roles, 1 S3 bucket).\n\n"
                            "## Suggested apply order\n1. Land SAFE additions first.\n"
                            "2. Drop the SG rule or scope to a known CIDR.\n"
                            "3. Re-plan and verify no destroys remain before the cluster change.\n\n"
                            "## Suggested follow-ups\n"
                            "- Add `prevent_destroy = true` lifecycle to `aws_rds_cluster.primary`.\n"
                            "- Add a Terraform Sentinel/OPA policy banning 0.0.0.0/0 SG ingress."
                        ),
                    },
                }
            )
        steps.append(
            {
                "final": (
                    "## Verdict\nREQUEST_CHANGES — 1 BLOCKING, 1 RISKY, 4 SAFE.\n\n"
                    "## Changes by class\n"
                    "- **BLOCKING** aws_rds_cluster.primary — destroy.\n"
                    "- **RISKY** aws_security_group.public — opens 0.0.0.0/0:22.\n"
                    "- **SAFE** 4 additive resources.\n\n"
                    "## Suggested apply order\nSAFE first; resolve BLOCKING before any destroy."
                )
            }
        )
        return steps
