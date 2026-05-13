"""Linear connector mock-mode tests."""

from __future__ import annotations

from pilothouse.connectors.base import ToolContext, registry


async def _emit(_n, _d):
    return None


def _ctx(dry_run: bool = True) -> ToolContext:
    return ToolContext(run_id="r", agent_id="a", dry_run=dry_run, params={}, emit=_emit)


def test_linear_tools_registered() -> None:
    tools = registry.all_tools()
    for name in (
        "linear_list_issues",
        "linear_get_issue",
        "linear_add_comment",
        "linear_update_status",
    ):
        assert name in tools
    # Read-only tools are not destructive; writes are.
    assert not tools["linear_list_issues"].is_destructive
    assert not tools["linear_get_issue"].is_destructive
    assert tools["linear_add_comment"].is_destructive
    assert tools["linear_update_status"].is_destructive


async def test_list_issues_mock_returns_node_list() -> None:
    tool = registry.all_tools()["linear_list_issues"]
    res = await tool.handler(_ctx(), {"team_key": "ENG", "label": "pilothouse-fix", "limit": 3})
    nodes = res.content["data"]["issues"]["nodes"]
    assert nodes
    assert all("identifier" in n for n in nodes)


async def test_get_issue_mock_includes_description_and_path() -> None:
    tool = registry.all_tools()["linear_get_issue"]
    res = await tool.handler(_ctx(), {"issue_id": "ENG-1234"})
    issue = res.content["data"]["issue"]
    assert issue["identifier"] == "ENG-1234"
    # Description should reference a file path so the bug fixer can read it.
    assert "services/users/api.py" in issue["description"]


async def test_add_comment_dry_run_short_circuits() -> None:
    tool = registry.all_tools()["linear_add_comment"]
    res = await tool.handler(
        _ctx(dry_run=True), {"issue_id": "ENG-1234", "content": "hi"}
    )
    assert res.content["dry_run"] is True
    assert "ENG-1234" in str(res.content["would_comment"]["issue_id"])
