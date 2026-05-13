"""GitHub connector.

Read tools are non-destructive. Write tools (anything that mutates the
repo: branches, files, PRs, reviews) are flagged destructive so they go
through dry-run and approval gating.

The write surface is what powers `bug_auto_fixer` (branch + file +
PR) and `pr_code_reviewer` (inline review). Each follows the same
shape: in dry-run we return a `would_have_called_with` preview without
hitting the API; in live mode we POST/PUT to the GitHub REST API.
"""

from __future__ import annotations

import base64
import hashlib
import random
from typing import Any

import httpx

from ..config import get_settings
from .base import Connector, ToolContext, ToolResult


class GitHubConnector(Connector):
    name = "github"

    def __init__(self) -> None:
        super().__init__()
        self._add(
            "github_get_pr",
            "Fetch a pull request by repo and number.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/repo"},
                    "pr_number": {"type": "integer"},
                },
                "required": ["repo", "pr_number"],
            },
            self._get_pr,
        )
        self._add(
            "github_get_pr_diff",
            "Fetch the unified diff for a pull request.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pr_number": {"type": "integer"},
                },
                "required": ["repo", "pr_number"],
            },
            self._get_pr_diff,
        )
        self._add(
            "github_list_recent_commits",
            "List the N most recent commits for a repo branch.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "branch": {"type": "string", "default": "main"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["repo"],
            },
            self._recent_commits,
        )
        self._add(
            "github_post_pr_comment",
            "Post a comment on a pull request. DESTRUCTIVE: writes to GitHub.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pr_number": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["repo", "pr_number", "body"],
            },
            self._post_pr_comment,
            is_destructive=True,
        )
        self._add(
            "github_get_file_content",
            "Read the content of a single file at a given ref (branch / sha).",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "path": {"type": "string"},
                    "ref": {"type": "string", "default": "main"},
                },
                "required": ["repo", "path"],
            },
            self._get_file_content,
        )
        self._add(
            "github_get_pr_files",
            "List files changed by a PR with per-file patch + status.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pr_number": {"type": "integer"},
                },
                "required": ["repo", "pr_number"],
            },
            self._get_pr_files,
        )
        self._add(
            "github_create_branch",
            "Create a branch from a ref. DESTRUCTIVE: writes to GitHub.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "branch": {"type": "string", "description": "new branch name"},
                    "from_ref": {
                        "type": "string",
                        "default": "main",
                        "description": "base ref (branch or sha)",
                    },
                },
                "required": ["repo", "branch"],
            },
            self._create_branch,
            is_destructive=True,
        )
        self._add(
            "github_create_or_update_file",
            "Create or update a file on a branch in one commit. DESTRUCTIVE.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "branch": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "raw file content"},
                    "message": {
                        "type": "string",
                        "description": "Conventional commit message, e.g. 'fix(payments): handle null'",
                    },
                },
                "required": ["repo", "branch", "path", "content", "message"],
            },
            self._create_or_update_file,
            is_destructive=True,
        )
        self._add(
            "github_create_pull_request",
            "Open a pull request from `head` into `base`. DESTRUCTIVE.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "head": {"type": "string", "description": "source branch"},
                    "base": {"type": "string", "default": "main", "description": "target branch"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "draft": {"type": "boolean", "default": False},
                },
                "required": ["repo", "head", "title", "body"],
            },
            self._create_pull_request,
            is_destructive=True,
        )
        self._add(
            "github_create_pr_review",
            (
                "Submit a structured review on a PR — top-level body + an "
                "optional list of inline comments. DESTRUCTIVE."
            ),
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pr_number": {"type": "integer"},
                    "event": {
                        "type": "string",
                        "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                        "default": "COMMENT",
                    },
                    "body": {"type": "string"},
                    "comments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "line": {"type": "integer"},
                                "side": {
                                    "type": "string",
                                    "enum": ["LEFT", "RIGHT"],
                                    "default": "RIGHT",
                                },
                                "body": {"type": "string"},
                            },
                            "required": ["path", "line", "body"],
                        },
                    },
                },
                "required": ["repo", "pr_number", "event", "body"],
            },
            self._create_pr_review,
            is_destructive=True,
        )

    @property
    def live(self) -> bool:
        return bool(get_settings().github_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {get_settings().github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get_pr(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_pr(params["repo"], params["pr_number"]))
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.github.com/repos/{params['repo']}/pulls/{params['pr_number']}",
                headers=self._headers(),
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _get_pr_diff(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_diff(params["repo"], params["pr_number"]))
        headers = self._headers() | {"Accept": "application/vnd.github.v3.diff"}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://api.github.com/repos/{params['repo']}/pulls/{params['pr_number']}",
                headers=headers,
            )
        return ToolResult(content=r.text, is_error=r.is_error)

    async def _recent_commits(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(content=_mock_commits(params["repo"], params.get("branch", "main")))
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.github.com/repos/{params['repo']}/commits",
                params={"sha": params.get("branch", "main"), "per_page": params.get("limit", 10)},
                headers=self._headers(),
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _post_pr_comment(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_post": {
                        "repo": params["repo"],
                        "pr_number": params["pr_number"],
                        "body_preview": params["body"][:200],
                    },
                }
            )
        if not self.live:
            return ToolResult(
                content={"error": "github_token not configured; cannot post comment"},
                is_error=True,
            )
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.github.com/repos/{params['repo']}/issues/{params['pr_number']}/comments",
                headers=self._headers(),
                json={"body": params["body"]},
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    # --- read tools added for the auto-fix / code-review flow -----------

    async def _get_file_content(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(
                content=_mock_file(params["repo"], params["path"], params.get("ref", "main"))
            )
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.github.com/repos/{params['repo']}/contents/{params['path']}",
                params={"ref": params.get("ref", "main")},
                headers=self._headers(),
            )
        if r.is_error:
            return ToolResult(content=r.json(), is_error=True)
        data = r.json()
        # GitHub returns base64-encoded content for files; decode for the LLM.
        if isinstance(data, dict) and data.get("encoding") == "base64":
            try:
                data["decoded_content"] = base64.b64decode(data["content"]).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                data["decoded_content"] = "(binary or undecodable)"
        return ToolResult(content=data)

    async def _get_pr_files(self, ctx: ToolContext, params: dict) -> ToolResult:
        if not self.live:
            return ToolResult(
                content=_mock_pr_files(params["repo"], params["pr_number"])
            )
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://api.github.com/repos/{params['repo']}/pulls/{params['pr_number']}/files",
                headers=self._headers(),
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    # --- write tools — branch / file / PR / review ---------------------

    async def _create_branch(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_create_branch": {
                        "repo": params["repo"],
                        "branch": params["branch"],
                        "from_ref": params.get("from_ref", "main"),
                    },
                }
            )
        if not self.live:
            return ToolResult(content={"error": "github_token not configured"}, is_error=True)
        from_ref = params.get("from_ref", "main")
        async with httpx.AsyncClient(timeout=15) as client:
            # Resolve ref → sha first.
            r = await client.get(
                f"https://api.github.com/repos/{params['repo']}/git/ref/heads/{from_ref}",
                headers=self._headers(),
            )
            if r.is_error:
                return ToolResult(content=r.json(), is_error=True)
            sha = r.json()["object"]["sha"]
            r2 = await client.post(
                f"https://api.github.com/repos/{params['repo']}/git/refs",
                headers=self._headers(),
                json={"ref": f"refs/heads/{params['branch']}", "sha": sha},
            )
        return ToolResult(content=r2.json(), is_error=r2.is_error)

    async def _create_or_update_file(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_commit": {
                        "repo": params["repo"],
                        "branch": params["branch"],
                        "path": params["path"],
                        "message": params["message"],
                        "content_preview": params["content"][:200],
                        "content_bytes": len(params["content"]),
                    },
                }
            )
        if not self.live:
            return ToolResult(content={"error": "github_token not configured"}, is_error=True)
        async with httpx.AsyncClient(timeout=20) as client:
            # If the file exists on this branch we need its current sha.
            existing = await client.get(
                f"https://api.github.com/repos/{params['repo']}/contents/{params['path']}",
                params={"ref": params["branch"]},
                headers=self._headers(),
            )
            sha = existing.json().get("sha") if existing.status_code == 200 else None
            body: dict[str, Any] = {
                "message": params["message"],
                "content": base64.b64encode(params["content"].encode("utf-8")).decode("ascii"),
                "branch": params["branch"],
            }
            if sha:
                body["sha"] = sha
            r = await client.put(
                f"https://api.github.com/repos/{params['repo']}/contents/{params['path']}",
                headers=self._headers(),
                json=body,
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _create_pull_request(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_open_pr": {
                        "repo": params["repo"],
                        "head": params["head"],
                        "base": params.get("base", "main"),
                        "title": params["title"],
                        "body_preview": params["body"][:300],
                        "draft": params.get("draft", False),
                    },
                }
            )
        if not self.live:
            return ToolResult(content={"error": "github_token not configured"}, is_error=True)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.github.com/repos/{params['repo']}/pulls",
                headers=self._headers(),
                json={
                    "title": params["title"],
                    "body": params["body"],
                    "head": params["head"],
                    "base": params.get("base", "main"),
                    "draft": params.get("draft", False),
                },
            )
        return ToolResult(content=r.json(), is_error=r.is_error)

    async def _create_pr_review(self, ctx: ToolContext, params: dict) -> ToolResult:
        if ctx.dry_run:
            return ToolResult(
                content={
                    "dry_run": True,
                    "would_post_review": {
                        "repo": params["repo"],
                        "pr_number": params["pr_number"],
                        "event": params["event"],
                        "body_preview": params["body"][:200],
                        "inline_comment_count": len(params.get("comments") or []),
                    },
                }
            )
        if not self.live:
            return ToolResult(content={"error": "github_token not configured"}, is_error=True)
        comments = []
        for c in params.get("comments") or []:
            comments.append(
                {
                    "path": c["path"],
                    "line": c["line"],
                    "side": c.get("side", "RIGHT"),
                    "body": c["body"],
                }
            )
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.github.com/repos/{params['repo']}/pulls/{params['pr_number']}/reviews",
                headers=self._headers(),
                json={
                    "event": params["event"],
                    "body": params["body"],
                    "comments": comments,
                },
            )
        return ToolResult(content=r.json(), is_error=r.is_error)


def _seed(*parts: Any) -> random.Random:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:8], 16))


def _mock_pr(repo: str, n: int) -> dict:
    rng = _seed("pr", repo, n)
    titles = [
        "Switch payment retries to exponential backoff",
        "Bump pillow 9.5.0 -> 10.0.1",
        "Add IAM policy for new analytics bucket",
        "Refactor checkout flow to use new pricing service",
        "Update connection pool size for checkout",
    ]
    return {
        "number": n,
        "title": rng.choice(titles),
        "user": {"login": rng.choice(["alice", "bob", "carla", "dan"])},
        "state": "open",
        "additions": rng.randint(20, 1200),
        "deletions": rng.randint(0, 400),
        "changed_files": rng.randint(1, 35),
        "base": {"ref": "main"},
        "head": {"ref": "feature/" + rng.choice(["payments", "iam", "checkout", "deps"])},
        "labels": [],
        "html_url": f"https://github.com/{repo}/pull/{n}",
    }


def _mock_diff(repo: str, n: int) -> str:
    rng = _seed("diff", repo, n)
    if rng.random() < 0.4:
        # secret-leak example to exercise security scanner
        return (
            "diff --git a/config/prod.env b/config/prod.env\n"
            "+++ b/config/prod.env\n"
            "@@ -1,3 +1,4 @@\n"
            " DATABASE_URL=postgres://prod\n"
            "+AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n"
            " REDIS_URL=redis://cache\n"
        )
    return (
        "diff --git a/services/checkout/pool.py b/services/checkout/pool.py\n"
        "@@ -10,7 +10,7 @@\n"
        "-POOL_SIZE = 20\n"
        "+POOL_SIZE = 80\n"
    )


def _mock_commits(repo: str, branch: str) -> list[dict]:
    rng = _seed("commits", repo, branch)
    return [
        {
            "sha": f"{rng.randrange(16**40):040x}",
            "commit": {
                "message": rng.choice(
                    [
                        "fix: bump pool size",
                        "feat: add retry budget",
                        "chore: bump deps",
                        "refactor: split checkout module",
                    ]
                ),
                "author": {
                    "name": rng.choice(["Alice", "Bob", "Carla"]),
                    "date": "2026-05-12T13:30:00Z",
                },
            },
        }
        for _ in range(8)
    ]


def _mock_file(repo: str, path: str, ref: str) -> dict:
    """Synthesise a small Python file so bug-fix templates have something
    to read in mock mode. Real file content would come from the GitHub
    contents API."""
    rng = _seed("file", repo, path, ref)
    sample = (
        "def get_user(user_id):\n"
        "    # NOTE: this throws on None; real bug we're fixing\n"
        '    return DB[user_id].copy()\n'
        "\n"
        "def list_users():\n"
        "    return [u for u in DB.values()]\n"
    )
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": f"{rng.randrange(16**40):040x}",
        "size": len(sample),
        "ref": ref,
        "encoding": "utf-8",
        "decoded_content": sample,
    }


def _mock_pr_files(repo: str, n: int) -> list[dict]:
    rng = _seed("pr_files", repo, n)
    return [
        {
            "filename": "services/checkout/pool.py",
            "status": "modified",
            "additions": 4,
            "deletions": 1,
            "changes": 5,
            "patch": (
                "@@ -10,7 +10,10 @@\n"
                "-POOL_SIZE = 20\n"
                "+# bumped after the saturation incident on 2026-05-10\n"
                "+POOL_SIZE = 80\n"
                "+POOL_TIMEOUT = 5  # was implicit; surface it\n"
            ),
        },
        {
            "filename": "tests/test_pool.py",
            "status": "added",
            "additions": 12,
            "deletions": 0,
            "changes": 12,
            "patch": (
                "@@ -0,0 +1,12 @@\n"
                "+def test_pool_uses_configured_size():\n"
                "+    assert POOL_SIZE == 80\n"
            ),
        },
    ]
