"""GitHub connector write-tool tests (dry-run gating + mock shapes)."""

from __future__ import annotations

from pilothouse.connectors.base import ToolContext, registry


async def _emit(_n, _d):
    return None


def _ctx(dry_run: bool = True) -> ToolContext:
    return ToolContext(run_id="r", agent_id="a", dry_run=dry_run, params={}, emit=_emit)


def test_new_github_tools_registered() -> None:
    tools = registry.all_tools()
    for name in (
        "github_get_file_content",
        "github_get_pr_files",
        "github_create_branch",
        "github_create_or_update_file",
        "github_create_pull_request",
        "github_create_pr_review",
    ):
        assert name in tools
    assert not tools["github_get_file_content"].is_destructive
    assert not tools["github_get_pr_files"].is_destructive
    assert tools["github_create_branch"].is_destructive
    assert tools["github_create_or_update_file"].is_destructive
    assert tools["github_create_pull_request"].is_destructive
    assert tools["github_create_pr_review"].is_destructive


async def test_get_file_content_mock_includes_decoded_content() -> None:
    tool = registry.all_tools()["github_get_file_content"]
    res = await tool.handler(
        _ctx(), {"repo": "acme/api", "path": "services/users/api.py", "ref": "main"}
    )
    assert "decoded_content" in res.content
    assert "def get_user" in res.content["decoded_content"]


async def test_get_pr_files_mock_returns_patch_per_file() -> None:
    tool = registry.all_tools()["github_get_pr_files"]
    res = await tool.handler(_ctx(), {"repo": "acme/api", "pr_number": 7})
    assert isinstance(res.content, list) and res.content
    for f in res.content:
        assert {"filename", "status", "patch"}.issubset(f.keys())


async def test_create_branch_dry_run() -> None:
    tool = registry.all_tools()["github_create_branch"]
    res = await tool.handler(
        _ctx(dry_run=True),
        {"repo": "acme/api", "branch": "pilothouse/fix/X-1-foo", "from_ref": "main"},
    )
    assert res.content["dry_run"] is True
    assert res.content["would_create_branch"]["branch"] == "pilothouse/fix/X-1-foo"


async def test_create_pr_dry_run_carries_title_and_body_preview() -> None:
    tool = registry.all_tools()["github_create_pull_request"]
    res = await tool.handler(
        _ctx(dry_run=True),
        {
            "repo": "acme/api",
            "head": "pilothouse/fix/X-1-foo",
            "base": "main",
            "title": "fix(users): handle null in get_user",
            "body": "Closes X-1\n\nReturns None on missing id.",
        },
    )
    assert res.content["dry_run"] is True
    assert "fix(users)" in res.content["would_open_pr"]["title"]
    assert "Closes X-1" in res.content["would_open_pr"]["body_preview"]


async def test_create_pr_review_dry_run_counts_inline_comments() -> None:
    tool = registry.all_tools()["github_create_pr_review"]
    res = await tool.handler(
        _ctx(dry_run=True),
        {
            "repo": "acme/api",
            "pr_number": 99,
            "event": "REQUEST_CHANGES",
            "body": "see inline",
            "comments": [
                {"path": "a.py", "line": 1, "body": "x"},
                {"path": "b.py", "line": 7, "body": "y"},
            ],
        },
    )
    assert res.content["dry_run"] is True
    assert res.content["would_post_review"]["inline_comment_count"] == 2
    assert res.content["would_post_review"]["event"] == "REQUEST_CHANGES"
