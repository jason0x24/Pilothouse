"""git_conventions helpers — pure functions, no IO."""

from __future__ import annotations

import pytest

from pilothouse.config import get_settings
from pilothouse.git_conventions import render_branch_spec, slugify


def test_slugify_basic() -> None:
    assert slugify("NPE in get_user when DB lookup misses") == "npe-in-get-user-when-db-lookup-misses"
    assert slugify("Fix: 504 on /checkout/retry!") == "fix-504-on-checkout-retry"
    assert slugify("   ") == "untitled"


def test_slugify_truncates_on_word_boundary() -> None:
    out = slugify("a-very-long-title-that-exceeds-the-default-budget-easily", max_len=20)
    assert len(out) <= 20
    # Should not end on a partial word.
    assert not out.endswith("-")


def test_render_branch_spec_default_prefix() -> None:
    spec = render_branch_spec(
        ticket_id="ENG-1234",
        title="NPE in get_user when DB lookup misses",
        summary="Customers hit a 500 on missing user; return None.",
        commit_type="fix",
        scope="users",
        files=["services/users/api.py"],
        agent_name="bug_auto_fixer",
    )
    assert spec.branch.startswith("pilothouse/fix/ENG-1234-")
    assert "npe-in-get-user" in spec.branch
    assert spec.commit_message.startswith("fix(users): ")
    assert "Closes ENG-1234" in spec.commit_message
    assert "Closes ENG-1234" in spec.pr_body
    assert "services/users/api.py" in spec.pr_body
    assert "🤖 Opened by Pilothouse" in spec.pr_body


def test_render_branch_spec_custom_prefix() -> None:
    spec = render_branch_spec(
        ticket_id="OPS-9",
        title="bump pool",
        summary="x",
        branch_prefix="bot",
    )
    assert spec.branch.startswith("bot/fix/OPS-9-bump-pool")


def test_render_branch_spec_subject_truncation() -> None:
    spec = render_branch_spec(
        ticket_id="ENG-1",
        title="a" * 200,
        summary="x",
    )
    subject = spec.commit_message.splitlines()[0]
    assert len(subject) <= 72


def test_invalid_commit_type_rejected() -> None:
    with pytest.raises(ValueError):
        render_branch_spec(
            ticket_id="X-1",
            title="t",
            summary="s",
            commit_type="hotfix",  # not in our allow-list
        )


def test_signoff_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_GIT_COMMIT_SIGNOFF", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        spec = render_branch_spec(
            ticket_id="ENG-1",
            title="t",
            summary="s",
            agent_name="bug_auto_fixer",
        )
        assert "Signed-off-by:" in spec.commit_message
    finally:
        monkeypatch.delenv("PILOTHOUSE_GIT_COMMIT_SIGNOFF", raising=False)
        get_settings.cache_clear()  # type: ignore[attr-defined]
