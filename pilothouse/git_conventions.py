"""Git workflow conventions used by auto-PR templates.

Centralised so policy lives in one place and is unit-testable. The
defaults below are the "Pilothouse house style":

  * **Branch**: `<prefix>/<type>/<TICKET-ID>-<slug>`
        e.g. `pilothouse/fix/ENG-1234-null-deref-in-get-user`
    The `<prefix>/` segment makes bot branches visually distinct from
    human branches and lets repo admins write protection rules like
    "anything under `pilothouse/` requires review from @sre".

  * **Commit message**: Conventional Commits.
        `<type>(<scope>): <subject>`
        <BLANK>
        <body>
        <BLANK>
        Closes <TICKET-ID>
        [Signed-off-by: Pilothouse <pilothouse@example.com>]

  * **PR title**: same as the commit subject line. PR body includes:
        - Linked ticket
        - Plain-English summary of the fix
        - Files touched
        - Test note ("ran X / cannot verify locally")
        - "🤖 Opened by Pilothouse <agent_name>" footer

The prefix can be overridden per-deploy via `PILOTHOUSE_GIT_BRANCH_PREFIX`,
and per-agent via the `branch_prefix` param.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import get_settings


# Conventional Commits types we use; templates pick one.
CommitType = str  # narrowed to: "fix" | "feat" | "chore" | "refactor" | "perf" | "test" | "docs"

_VALID_TYPES = {"fix", "feat", "chore", "refactor", "perf", "test", "docs"}


def slugify(text: str, *, max_len: int = 40) -> str:
    """Lowercase + collapse whitespace + strip non-alphanumerics → kebab.

    Used to turn an issue title ("NPE in get_user when DB lookup misses")
    into a branch-safe slug ("npe-in-get-user-when-db-lookup-misses").
    """
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        # Cut on a word boundary if we can.
        cut = s[:max_len].rsplit("-", 1)[0]
        s = cut or s[:max_len]
    return s or "untitled"


@dataclass
class BranchSpec:
    """Resolved branch + commit + PR strings for one auto-fix."""

    branch: str
    commit_message: str
    pr_title: str
    pr_body: str


def render_branch_spec(
    *,
    ticket_id: str,
    title: str,
    summary: str,
    commit_type: CommitType = "fix",
    scope: str = "",
    body: str = "",
    files: list[str] | None = None,
    test_note: str = "",
    agent_name: str = "pilothouse",
    branch_prefix: str | None = None,
) -> BranchSpec:
    """Compute the branch/commit/PR strings for an auto-fix.

    `ticket_id` may include the team prefix (`ENG-1234`) or be just an
    arbitrary string — it gets uppercased and stamped at branch start +
    in the `Closes` footer.
    """
    if commit_type not in _VALID_TYPES:
        raise ValueError(
            f"unknown commit type {commit_type!r}; expected one of {sorted(_VALID_TYPES)}"
        )

    settings = get_settings()
    prefix = branch_prefix or settings.git_branch_prefix
    ticket = ticket_id.strip().upper()
    slug = slugify(title)
    branch = f"{prefix}/{commit_type}/{ticket}-{slug}" if ticket else f"{prefix}/{commit_type}/{slug}"

    subject_scope = f"({scope})" if scope else ""
    subject = f"{commit_type}{subject_scope}: {title.strip()}"
    if len(subject) > 72:
        # Conventional commits style caps the subject at ~72 chars.
        subject = subject[:69] + "…"

    commit_lines = [subject]
    if body:
        commit_lines += ["", body.strip()]
    if ticket:
        commit_lines += ["", f"Closes {ticket}"]
    if settings.git_commit_signoff:
        commit_lines += ["", f"Signed-off-by: Pilothouse <pilothouse+{agent_name}@local>"]
    commit_message = "\n".join(commit_lines)

    pr_title = subject
    pr_body_lines = [summary.strip(), ""]
    if files:
        pr_body_lines += ["**Files touched**"]
        pr_body_lines += [f"- `{p}`" for p in files]
        pr_body_lines += [""]
    if test_note:
        pr_body_lines += ["**Tests**", test_note.strip(), ""]
    if ticket:
        pr_body_lines += [f"Closes {ticket}", ""]
    pr_body_lines += [
        "---",
        f"🤖 Opened by Pilothouse agent `{agent_name}`. ",
        "Review carefully — destructive operations were gated by approval; "
        "code suggestions were not.",
    ]
    pr_body = "\n".join(pr_body_lines).rstrip()

    return BranchSpec(
        branch=branch,
        commit_message=commit_message,
        pr_title=pr_title,
        pr_body=pr_body,
    )
