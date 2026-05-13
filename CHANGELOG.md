# Pilothouse changelog

All shipped features, organised by the phase that delivered them. Pre-1.0
the platform is single-development-line; see `git log` for finer grain.

## Unreleased

### Execution backends

- **Temporal executor (optional)** — set `PILOTHOUSE_TEMPORAL_ADDRESS`
  to switch from the in-process asyncio executor to a Temporal-backed
  one. Three modes:
    * `dev` — embedded Temporal server in the same process. Durable
      workflows on one box, no infra required.
    * `host:port` — connect to an existing Temporal cluster for
      horizontal worker scaling + cross-restart durability.
  Each Run becomes a `PilothouseAgentRun` workflow that drives
  `run_agent_activity` / `resume_run_activity` / `cancel_run_activity`.
  Cancellation + approval routing go through workflow signals.
  Dependency is optional: `pip install 'pilothouse[temporal]'`.
- **Orchestration refactored into executor interface** —
  `pilothouse/orchestration/_inprocess.py` (default),
  `pilothouse/orchestration/_temporal.py` (Temporal),
  `pilothouse/orchestration/_common.py` (shared persist/hook helpers).
  Public surface (`execute_agent`, `resume_run`, `cancel_run`,
  `retry_run`, `sweep_expired_approvals`) is unchanged — every existing
  import path keeps working.
- **`pilothouse temporal status` CLI** — shows the active executor
  mode + Temporal address/namespace/task queue when configured.

### Plugin ecosystem

- **SIEM audit-forwarding HookPlugin example**
  (`examples/plugins/siem_audit_forwarder.py`) — pattern for
  forwarding every Run start/finish into an external SIEM with bounded
  in-memory buffering, drop-oldest backpressure, and a background
  drainer so the orchestration path never blocks on slow HTTP.

## 0.1.0 — initial release

### Foundations

- Async SQLAlchemy 2.0 + SQLite default; Postgres swap via env var.
- Anthropic tool-use loop runtime with deterministic mock-mode replay
  for keyless development.
- Connector / tool framework with five built-ins (Datadog, GitHub,
  PagerDuty, Slack, Kubernetes) — each in live + mock modes.
- Six agent templates: Datadog Alert Triage, PR Security Scanner, K8s
  Pod Investigator, Terraform Plan Reviewer, PagerDuty First Responder,
  Flaky Test Hunter.
- FastAPI server with management API + per-source webhook receivers.
- Click-based CLI (`pilothouse`).
- Next.js 15 console with Tailwind UI.

### Safety + audit

- `dry_run` short-circuits destructive tools at the runtime layer.
- Approval flow: `pause-on-destructive → operator decides → resume`.
- Approval TTL sweeper auto-rejects stale gates and resumes the run.
- Bulk approve/reject via `POST /approvals/resolve-batch` + CLI.
- Append-only `events` audit log; per-run JSON + CSV export.
- SSE event stream (`/runs/{id}/events/stream`) replays + tails live.

### Triggers + lifecycle

- Cron scheduling via APScheduler; `/schedule` lists next-fire times.
- Webhook receivers with real per-source signature verification
  (GitHub `X-Hub-Signature-256`, Slack v0 + 5-minute window, PagerDuty
  multi-key, Datadog `DD-Signature`).
- Run cancel + retry endpoints/CLI.
- Trigger deduplication (TTL-windowed payload digest) so retried
  webhooks don't trigger duplicate runs.
- Per-tenant sliding-window rate limit.

### Multi-tenancy

- `Tenant` model + automatic "default" bootstrap; existing rows backfilled.
- API key resolution per tenant; cross-tenant lookups return 404.
- Per-tenant quotas (`max_agents`, `max_runs_per_day`).
- `pilothouse tenants {list,create,add-key,remove-key,set-quota,show-keys,delete}`.

### Operator UX

- Slack-native approvals: notification messages include Approve / Reject
  buttons; clicking POSTs to a verified `/webhooks/slack/interactivity`
  endpoint that resolves and updates the message in-place.
- Approval + failure notifications via Slack channel param +
  `PILOTHOUSE_NOTIFY_WEBHOOK_URL`.
- `pilothouse runs logs <id>` colored timeline.
- Pretty `pilothouse plan/apply/export` for declarative GitOps agent
  management (`agents.yaml`).
- Console: dashboard, agents (search + template filter), runs (status /
  agent / trigger / summary search + pagination), run detail with live
  SSE timeline + Cancel/Retry, approvals (bulk select), schedule, system.

### Ecosystem

- MCP adapter — register any stdio or HTTP MCP server as a Pilothouse
  connector. Tool destructive-flag honoured via `inputSchema.x-destructive`.
- DB persistence + lifespan re-attach for MCP servers.

### Operations

- Per-tenant in-memory bus + Prometheus `/metrics` (events, tools,
  approvals, run statuses, agent counts, pending approvals).
- API key auth middleware (`PILOTHOUSE_API_KEYS` legacy env still seeds
  the default tenant).
- Multi-stage `Dockerfile` (~150 MB, non-root user) + `docker-compose.yml`
  bringing up api + console + Postgres in one command.
- GitHub Actions CI: ruff lint + pytest + console build + Docker image
  smoke test.
