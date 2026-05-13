"""Centralised configuration loaded from env / .env.

Pilothouse is multi-tenant in principle but the MVP runs single-tenant: one
process, one database, one operator. All defaults are dev-safe — secrets stay
empty so the runtime falls back to "mock" connectors that don't hit any
external API. Set the corresponding env var to switch a connector to live.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PILOTHOUSE_",
        extra="ignore",
    )

    # Core
    data_dir: Path = Field(default=Path("./var"))
    database_url: str = "sqlite+aiosqlite:///./var/pilothouse.db"
    log_level: str = "INFO"

    # LLM provider selection.
    #
    # Real LLM calls go through LiteLLM (https://docs.litellm.ai/), which
    # speaks 100+ APIs (Anthropic, OpenAI, OpenRouter, Bedrock, Vertex,
    # Gemini, Groq, Mistral, Together, Azure, Cohere, Ollama, …). Routing
    # is driven by `model_planner` / `model_worker` prefixes; see
    # docs/USER_GUIDE.md §9 for the full table.
    #
    # `model_provider`:
    #   ""        — auto: LiteLLM if any LLM key is set, else mock
    #   "litellm" — force LiteLLM (operator may use shell env vars LiteLLM
    #               picks up that we don't enumerate)
    #   "mock"    — force deterministic replay (no network)
    model_provider: str = ""

    # API keys for the providers LiteLLM auto-detects via env. Set any
    # of these and Pilothouse plants them into LiteLLM's expected env
    # names (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY) so
    # model-id prefix routing works. Cloud providers with multi-var auth
    # (Bedrock = AWS_*, Vertex = GOOGLE_APPLICATION_CREDENTIALS, Azure)
    # are picked up directly from the shell.
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    openai_api_key: str = ""

    # Self-hosted OpenAI-compat endpoints (vLLM, LM Studio, Together,
    # Groq, Mistral, …). When set, passed to LiteLLM as `api_base` for
    # openai/* model ids.
    openai_base_url: str = ""

    # Optional OpenRouter attribution headers (X-Title / HTTP-Referer)
    # surfaced to the openrouter/* request. Used for rate-limit tiers.
    openrouter_app_name: str = ""
    openrouter_site_url: str = ""

    # Model ids — must match LiteLLM's catalogue. Defaults are Anthropic
    # native; for OpenRouter use e.g. "openrouter/anthropic/claude-sonnet-4-5"
    # or "openrouter/openai/gpt-4o".
    model_planner: str = "claude-opus-4-5"
    model_worker: str = "claude-haiku-4-5"
    max_tool_iterations: int = 12
    max_output_tokens: int = 4096

    # Safety
    dry_run_default: bool = True  # write tools won't execute unless explicitly enabled
    require_approval_for_writes: bool = True

    # Server
    host: str = "127.0.0.1"
    port: int = 8088
    webhook_secret: str = ""  # if set, webhook receivers verify HMAC-SHA256
    # Comma-separated list of API keys. When non-empty, every endpoint
    # except /healthz, /metrics, and /webhooks/* requires
    # `Authorization: Bearer <key>` or `X-API-Key: <key>` to match.
    api_keys: str = ""
    metrics_enabled: bool = True

    # Approvals expire after this many minutes; the background sweeper
    # auto-rejects them and resumes the run with a timeout reason.
    approval_ttl_minutes: int = 60
    approval_sweep_interval_seconds: int = 30

    # Trigger deduplication. When > 0, two webhook/manual triggers with
    # the same agent_id + payload digest within the window are coalesced
    # — only the first creates a Run, subsequent ones return the same id.
    # Set to 0 to disable.
    dedup_window_seconds: int = 60

    # Per-tenant rate limit (triggers per minute, sliding window). 0 = off.
    rate_limit_per_minute: int = 60

    # Temporal executor. Three modes:
    #   ""             — in-process executor (default; no Temporal needed)
    #   "dev"          — boot an in-process Temporal dev server.
    #                    Durable workflows + signals, no external infra.
    #   "<host:port>"  — connect to an existing Temporal cluster.
    # When non-empty, the temporalio package must be installed:
    #   pip install 'pilothouse[temporal]'
    temporal_address: str = ""
    temporal_namespace: str = "default"
    temporal_task_queue: str = "pilothouse"

    # Connectors (empty = mock mode)
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    github_token: str = ""
    pagerduty_token: str = ""
    slack_bot_token: str = ""
    # Linear: a personal API key (`lin_api_…`) or OAuth access token,
    # used by the bug_auto_fixer template to read issues and comment back.
    linear_api_key: str = ""
    # Kubernetes: bearer-token + API URL. The token typically comes from a
    # ServiceAccount mounted at /var/run/secrets/kubernetes.io/serviceaccount.
    kube_api_url: str = ""  # e.g. https://kubernetes.default.svc
    kube_token: str = ""
    kube_ca_path: str = ""  # path to CA bundle, optional

    # Git workflow conventions for auto-PR templates. Defaults reflect
    # the recommended Pilothouse style; override per-deployment to match
    # an existing org policy.
    git_branch_prefix: str = "pilothouse"  # → branches like pilothouse/fix/ENG-1234-…
    git_commit_signoff: bool = False  # add a "Signed-off-by" trailer (DCO-friendly)
    git_pr_draft: bool = False  # open auto-PRs as drafts

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def parsed_api_keys(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
