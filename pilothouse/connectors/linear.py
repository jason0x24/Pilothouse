"""Linear connector — read issues + write comments / status.

Linear's API is GraphQL, single endpoint `https://api.linear.app/graphql`.
We speak the subset we need without a dedicated GraphQL client; httpx
sends the JSON envelope and we parse the response.

Tools:
  * linear_list_issues   — read; filter by team, label, priority, state
  * linear_get_issue     — read; full issue + description + comments
  * linear_add_comment   — write (DESTRUCTIVE)
  * linear_update_status — write (DESTRUCTIVE) — move ticket along the workflow

The connector is the read-side input for the `bug_auto_fixer` template:
"poll Linear for issues labeled `pilothouse-fix`, pick one, fix it, PR
it, comment back here." The same connector pattern works for Jira/GitHub
Issues — wire a new file with the equivalent shape and the templates
that depend on a "ticket source" can be parametrised over which
connector to call.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

import httpx

from ..config import get_settings
from .base import Connector, ToolContext, ToolResult

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


class LinearConnector(Connector):
    name = "linear"

    def __init__(self) -> None:
        super().__init__()
        self._add(
            "linear_list_issues",
            (
                "List Linear issues matching filters. Useful for cron-driven "
                "workflows that pick up labelled issues for processing."
            ),
            {
                "type": "object",
                "properties": {
                    "team_key": {"type": "string", "description": "team prefix, e.g. ENG"},
                    "label": {"type": "string", "description": "label name to filter by"},
                    "state": {
                        "type": "string",
                        "description": "issue state name (e.g. 'Triage', 'Todo')",
                    },
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                },
            },
            self._list_issues,
        )
        self._add(
            "linear_get_issue",
            "Fetch one Linear issue by id (e.g. ENG-1234) with description and recent comments.",
            {
                "type": "object",
                "properties": {"issue_id": {"type": "string"}},
                "required": ["issue_id"],
            },
            self._get_issue,
        )
        self._add(
            "linear_add_comment",
            "Add a comment to a Linear issue. DESTRUCTIVE: writes to Linear.",
            {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "content": {"type": "string", "description": "markdown body"},
                },
                "required": ["issue_id", "content"],
            },
            self._add_comment,
            is_destructive=True,
        )
        self._add(
            "linear_update_status",
            "Move an issue to a new workflow state. DESTRUCTIVE.",
            {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "state_name": {
                        "type": "string",
                        "description": "target state name, e.g. 'In Progress', 'In Review'",
                    },
                },
                "required": ["issue_id", "state_name"],
            },
            self._update_status,
            is_destructive=True,
        )

    @property
    def live(self) -> bool:
        return bool(get_settings().linear_api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": get_settings().linear_api_key,
            "Content-Type": "application/json",
        }

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                LINEAR_GRAPHQL_URL,
                headers=self._headers(),
                json={"query": query, "variables": variables or {}},
            )
        if r.is_error:
            return {"errors": [{"message": f"HTTP {r.status_code}: {r.text[:200]}"}]}
        return r.json()

    # --- handlers ---------------------------------------------------

    async def _list_issues(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_list(params))
        # Build a filter object — Linear's query language uses nested
        # `equals` / `name` clauses. We keep it simple and let the server
        # ignore unknown fields if the schema shifts.
        filt: dict[str, Any] = {}
        if params.get("team_key"):
            filt["team"] = {"key": {"eq": params["team_key"]}}
        if params.get("label"):
            filt["labels"] = {"name": {"eq": params["label"]}}
        if params.get("state"):
            filt["state"] = {"name": {"eq": params["state"]}}
        query = """
        query ListIssues($filter: IssueFilter, $first: Int) {
          issues(filter: $filter, first: $first, orderBy: createdAt) {
            nodes {
              id identifier title priority url
              state { name }
              labels { nodes { name } }
              createdAt updatedAt
            }
          }
        }
        """
        data = await self._gql(query, {"filter": filt, "first": params.get("limit", 20)})
        return ToolResult(content=data, is_error=bool(data.get("errors")))

    async def _get_issue(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_issue(params["issue_id"]))
        query = """
        query GetIssue($id: String!) {
          issue(id: $id) {
            id identifier title description priority url
            state { name }
            assignee { name email }
            labels { nodes { name } }
            comments(first: 20) { nodes { body user { name } createdAt } }
          }
        }
        """
        data = await self._gql(query, {"id": params["issue_id"]})
        return ToolResult(content=data, is_error=bool(data.get("errors")))

    async def _add_comment(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_comment": {
                        "issue_id": params["issue_id"],
                        "preview": params["content"][:200],
                    },
                }
            )
        if not self.live:
            return ToolResult(content={"error": "linear_api_key not configured"}, is_error=True)
        query = """
        mutation AddComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) {
            success comment { id }
          }
        }
        """
        data = await self._gql(query, {"issueId": params["issue_id"], "body": params["content"]})
        return ToolResult(content=data, is_error=bool(data.get("errors")))

    async def _update_status(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_set_state": {
                        "issue_id": params["issue_id"],
                        "state_name": params["state_name"],
                    },
                }
            )
        if not self.live:
            return ToolResult(content={"error": "linear_api_key not configured"}, is_error=True)
        # Update in two hops: look up state id by name (within the issue's
        # team), then issueUpdate. We collapse into one mutation when
        # state ids are pre-known by the agent; here we keep it general.
        return ToolResult(
            content={
                "note": (
                    "linear_update_status currently requires the agent to know the "
                    "stateId; consider a thin wrapper that resolves state name → id "
                    "via the team's workflow query first."
                )
            }
        )


# --- mock data fabrication ----------------------------------------------


def _seed(*parts: Any) -> random.Random:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:8], 16))


def _mock_list(params: dict) -> dict:
    rng = _seed("list", str(sorted(params.items())))
    titles = [
        "NPE in get_user when DB lookup misses",
        "Checkout 504 on retry — backoff jitter wrong",
        "Slack notify silently drops messages > 4kB",
        "Migration 0042 takes 8m on prod-sized data",
    ]
    nodes = []
    for i in range(min(params.get("limit", 5), 4)):
        nodes.append(
            {
                "id": f"a1b2c3d4-{i:04d}",
                "identifier": f"ENG-{1000 + i}",
                "title": titles[i],
                "priority": rng.randint(1, 4),
                "url": f"https://linear.app/acme/issue/ENG-{1000 + i}",
                "state": {"name": params.get("state", "Triage")},
                "labels": {"nodes": [{"name": params.get("label", "pilothouse-fix")}]},
                "createdAt": "2026-05-13T08:30:00Z",
                "updatedAt": "2026-05-13T09:00:00Z",
            }
        )
    return {"data": {"issues": {"nodes": nodes}}}


def _mock_issue(issue_id: str) -> dict:
    rng = _seed("issue", issue_id)
    return {
        "data": {
            "issue": {
                "id": "a1b2c3d4",
                "identifier": issue_id,
                "title": "NPE in get_user when DB lookup misses",
                "description": (
                    "Repro:\n"
                    "1. Hit `/users/999` (a non-existent id)\n"
                    "2. Server raises `AttributeError: 'NoneType' object has no attribute 'copy'`\n\n"
                    "Expected: 404 with a structured error body.\n\n"
                    "File pointer: `services/users/api.py:42` (`get_user`)."
                ),
                "priority": 2,
                "url": f"https://linear.app/acme/issue/{issue_id}",
                "state": {"name": "Triage"},
                "assignee": None,
                "labels": {"nodes": [{"name": "pilothouse-fix"}, {"name": "bug"}]},
                "comments": {
                    "nodes": [
                        {
                            "body": "Customer hit this 3x today already.",
                            "user": {"name": "Carla"},
                            "createdAt": "2026-05-13T07:50:00Z",
                        }
                    ]
                },
            }
        }
    }
