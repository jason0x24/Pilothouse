# Pilothouse

**English** · [简体中文](./README.zh-CN.md)

> AI DevOps Copilot platform — configure an agent once, let it run on
> triggers across CI/CD, monitoring and IaC.

Pilothouse is a multi-tenant, production-grade platform for shipping
LLM-powered automations into a DevOps stack. Operators register
**agents** (a name + a template + params + a trigger). From then on,
those agents fire automatically — on cron, on webhook, or via API — to
investigate alerts, review PRs, diagnose pod failures, run a Terraform
plan review, and post their findings back. Every destructive action is
gated by either a dry-run preview or an explicit human approval; every
LLM step is captured in an append-only audit log.

```
[ webhooks ]                                         ┌──────────────┐
[ cron     ] ──► FastAPI server ──► AgentRunner ──► │ Tool registry │
[ CLI / UI ]      (multi-tenant)    (tool-use loop) │ • Datadog     │
                                          │         │ • GitHub      │
                                          ▼         │ • PagerDuty   │
                              SQLite / Postgres     │ • Slack       │
                              agents · runs ·       │ • Kubernetes  │
                              events · approvals ·  │ • MCP (any)   │
                              tenants · mcp_servers └──────────────┘
                                          │
                              SSE • Prometheus • Slack/webhook notify
                              JSON/CSV audit export • cost dashboard
```

---

## Quick start

```bash
# Option A — local Python via uv (recommended; manages Python + deps + lock)
curl -LsSf https://astral.sh/uv/install.sh | sh    # one-time
uv sync --all-extras                               # reads .python-version (3.12) + pyproject
uv run pilothouse db init
uv run pilothouse demo                             # one-of-each agent + mock run
uv run pilothouse serve                            # http://127.0.0.1:8088
cd console && npm install && npm run dev           # http://localhost:3000

# Option B — local Python via pip (traditional)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,temporal]'
pilothouse db init && pilothouse demo

# Option C — full stack via Docker (api + console + Postgres)
docker compose up
# api      http://localhost:8088
# console  http://localhost:3000
```

`pilothouse demo` creates one agent per template and triggers each in
mock mode, printing the full audit trail. **No API keys required**.
`uv.lock` is committed so every contributor and CI run gets the same
dependency tree.

### Documentation

| Audience | Doc |
|---|---|
| **Operators** running Pilothouse | [User Guide](./docs/USER_GUIDE.md) — install, configure, trigger, approve, deploy |
| **Plugin authors** extending Pilothouse | [Plugin Authoring Guide](./docs/PLUGIN_AUTHORING.md) — 5 kinds, scaffolding, testing, distribution |
| **Quick CLI reference** | `pilothouse --help`, `pilothouse <subcommand> --help` |
| **HTTP API reference** | OpenAPI at `/docs` once `pilothouse serve` is running |

---

## Implemented features

### 1. Plugin system

The whole platform is plugin-based. Every template, connector,
notifier, trigger, and lifecycle hook — including the ones shipped in
the wheel — is registered through a single `PluginManager`. Operators
toggle them at runtime with no code changes:

| | |
|---|---|
| **Five plugin kinds** | `TemplatePlugin` (new agent templates) · `ConnectorPlugin` (external service tools) · `NotifierPlugin` (event-bus subscribers) · `TriggerPlugin` (new ways to fire agents) · `HookPlugin` (`before_run` / `after_run` lifecycle) — a plugin can mix kinds via multiple inheritance. |
| **Three discovery paths** | **In-tree built-ins** (the 8 templates + 6 connectors that ship with the wheel) · **Entry points** (`[project.entry-points."pilothouse.plugins"]` in any pip-installed package) · **Directory scan** (`PILOTHOUSE_PLUGIN_DIR` or `./plugins/` for local dev). |
| **Persisted enable/disable** | `pilothouse plugins disable builtin.template.flaky_test_hunter` flips a row in the `plugins` table. Disabled plugins stay disabled across restarts and are removed from the live registries. |
| **Live re-discovery** | `pilothouse plugins reload` (or `POST /plugins/reload`) re-scans entry points + directory without a process restart — useful after dropping a new plugin file. |
| **Declared config schema** | Plugins declare required / optional config fields (with `secret`, `default`, `env_fallback`); operators set values via CLI / HTTP / UI; the manager validates at activation and marks plugins **misconfigured** instead of crashing. `pilothouse plugins doctor` exits non-zero if anything is missing — perfect for CI pre-deploy checks. |
| **Same API for built-ins and third parties** | Built-ins are just `BuiltinTemplatePlugin` / `BuiltinConnectorPlugin` adapters. An organisation can ship its private templates as a pip package and they get loaded the same way. |
| **Console UI** | `/plugins` route — list, filter by kind / status, enable/disable, edit config inline (secrets masked). Banner at top flags misconfigured plugins. |
| **Authoring guide + examples** | `examples/plugins/discord_notifier.py` (notifier with config schema) · `examples/plugins/poll_url_trigger.py` (trigger with start/stop lifecycle) · `examples/plugins/README.md`. |

CLI surface:

```bash
pilothouse plugins list                              # discover + show all with status
pilothouse plugins info <name> [--reveal]            # one-plugin detail (meta + schema + config)
pilothouse plugins enable  / disable <name>          # round-trip persists
pilothouse plugins reload                            # re-scan entry points + directory
pilothouse plugins doctor                            # exit 1 if any plugin is misconfigured
pilothouse plugins config show <name> [--reveal]    # masked by default
pilothouse plugins config set   <name> <key> <value>
pilothouse plugins config unset <name> <key>
pilothouse plugins scaffold <kind> <name>            # generate starter plugin + tests + pytest.ini
pilothouse plugins install <package>                 # pip install + reload (entry-point plugins)
```

HTTP surface (admin tenant only): `GET /plugins`,
`POST /plugins/{name}/{enable,disable}`, `POST /plugins/reload`,
`GET /plugins/doctor`, `GET/POST /plugins/{name}/config`.

**Authoring a plugin?** 30-second loop:

```bash
pilothouse plugins scaffold notifier my_discord     # writes plugin.py + test + pytest.ini
PILOTHOUSE_PLUGIN_DIR=./plugins pilothouse plugins reload
pilothouse plugins info my_discord                  # see required config
pytest plugins/tests/                               # async tests work out of the box
```

Full author guide: [`docs/PLUGIN_AUTHORING.md`](./docs/PLUGIN_AUTHORING.md).
Testing helpers live in `pilothouse.testing`:
`mock_tool_context`, `make_event`, `capture_events`, `temp_plugin_manager`.

### 2. Agent runtime

| | |
|---|---|
| **Tool-use loop** | Anthropic's tool-use protocol; each turn is checkpointable so a run can be paused at an approval gate and resumed in a different process. |
| **Mock mode** | When `PILOTHOUSE_ANTHROPIC_API_KEY` is empty the runtime walks each template's `mock_plan()` deterministically — entire stack runs offline for tests/demos. |
| **Resumable state** | `Run.state_json` carries the serialised loop state (messages, pending tool calls, partial results). |
| **Cooperative cancellation** | The loop checks `Run.status` between iterations; an external `cancel_run` cleanly exits with the audit trail intact. |
| **Cost tracking** | Per-run `tokens_input` / `tokens_output` / `cost_usd_cents` surfaced in the dashboard and Prometheus metrics. |

### 3. Built-in templates

The product surface — eight shippable playbooks:

| Template | Trigger | What it does |
|---|---|---|
| `datadog_alert_triage` | Datadog webhook / PagerDuty | Pull alert + metric + recent deploys + logs → ranked triage report |
| `pr_security_scanner` | GitHub PR webhook | Diff scan for secrets, dep bumps, IAM, migrations → REQUEST_CHANGES comment |
| `k8s_pod_investigator` | Alertmanager | `describe pod` + events + previous-container logs → top 5 ranked causes |
| `terraform_plan_reviewer` | GitHub PR webhook | Classify each change as BLOCKING / RISKY / SAFE → review comment |
| `pagerduty_first_responder` | PagerDuty webhook | Gather incident context, post to Slack, add note. **Does not ack** — that's a human call |
| `flaky_test_hunter` | Cron (nightly) | Scan recent CI for tests that pass→fail→pass → tracking-issue digest |
| `bug_auto_fixer` | Cron / Linear webhook | Pick up a tagged Linear issue → read referenced file → branch + commit + PR using the conventional git workflow → comment back |
| `pr_code_reviewer` | GitHub PR webhook | Multi-dimensional review (correctness / performance / readability / security / tests) → one structured GitHub review with **inline line-anchored comments** |

### 4. Connectors

Real HTTP calls in **live** mode, deterministic synthetic responses in
**mock** mode (so templates can be developed without credentials).

| Connector | Tools | Live mode env var |
|---|---|---|
| `datadog` | query_metric, get_alert, recent_deploys, search_logs | `_DATADOG_API_KEY` + `_APP_KEY` |
| `github` | get_pr, get_pr_diff, get_pr_files, get_file_content, list_recent_commits, post_pr_comment ⚠, create_branch ⚠, create_or_update_file ⚠, create_pull_request ⚠, create_pr_review ⚠ | `_GITHUB_TOKEN` |
| `pagerduty` | get_incident, add_note ⚠, acknowledge ⚠ | `_PAGERDUTY_TOKEN` |
| `slack` | post_message ⚠ | `_SLACK_BOT_TOKEN` |
| `kubernetes` | describe_pod, get_pod_events, get_pod_logs, list_pods | `_KUBE_API_URL` + `_TOKEN` |
| `linear` | list_issues, get_issue, add_comment ⚠, update_status ⚠ | `_LINEAR_API_KEY` |
| `mcp` | _any tools exposed by a registered MCP server_ | n/a |

⚠ = destructive (subject to dry-run + approval gate).

### 5. MCP adapter

Register **any** Model Context Protocol server as a Pilothouse
connector — its tools appear alongside the built-ins, with the same
dry-run and approval gates.

```bash
# stdio transport (uvx / npx-launched servers)
pilothouse connectors add-mcp time uvx mcp-server-time

# HTTP transport (hosted MCP servers)
pilothouse connectors add-mcp finance --http https://mcp.example/rpc \
    --header "Authorization=Bearer $TOKEN"

# Mark specific tools as destructive (gated)
pilothouse connectors add-mcp ops mcp-ops \
    --destructive delete_user --destructive drop_table
```

MCP server registrations are persisted in `mcp_servers` and re-attached
on server startup. Tool destructive-flag is honoured automatically when
the upstream's `inputSchema.x-destructive` is `true`.

### 6. Triggers

| Trigger | Setup |
|---|---|
| **Manual (CLI)** | `pilothouse agents trigger <id> --file payload.json` |
| **Manual (HTTP)** | `POST /agents/{id}/trigger` with `{"payload": {...}}` |
| **Cron** | Set `schedule_cron` on an agent; APScheduler fires it |
| **Webhook (Datadog)** | `POST /webhooks/datadog/{agent_id}` — verified via `DD-Signature` |
| **Webhook (GitHub)** | `POST /webhooks/github/{agent_id}` — verified via `X-Hub-Signature-256` |
| **Webhook (PagerDuty)** | `POST /webhooks/pagerduty/{agent_id}` — verified via `X-PagerDuty-Signature` (multi-key for rotation) |
| **Webhook (Slack)** | `POST /webhooks/slack/{agent_id}` — verified via Slack v0 + 5-min window |
| **Webhook (Alertmanager)** | `POST /webhooks/alertmanager/{agent_id}` |
| **Webhook (generic)** | `POST /webhooks/generic/{agent_id}` — generic HMAC-SHA256 |

Each source has its own secret env var (`PILOTHOUSE_*_WEBHOOK_SECRET`)
so keys can be rotated independently. Empty secret = verification
disabled (dev default).

### 7. Safety gates

Every destructive tool call passes through three layers:

1. **`dry_run`** — when on, destructive tools never execute; the
   runtime returns a `would_have_called_with: {…}` preview instead.
   Enforced at the runtime, even if a connector forgets.
2. **Approval gate** — when `dry_run=false` and
   `require_approval_for_writes=true` (default), the runtime pauses,
   creates an `Approval` row carrying the proposed input + assistant
   rationale, and exits. A separate `resume_run` re-enters with the
   operator's decision injected.
3. **Approval TTL** — pending approvals older than
   `PILOTHOUSE_APPROVAL_TTL_MINUTES` (default 60) are auto-rejected
   by a background sweeper and the run resumes with a structured
   `expired` rejection result fed to the model. Nothing dangles forever.

Approval routing:

- **Slack-native interactive approval** — `notify_slack_channel: "#sre"`
  on the agent posts a block-kit message with Approve / Reject buttons.
  Clicking POSTs back to `/webhooks/slack/interactivity` (verified) →
  resolves the approval → updates the message in-place. No need to
  leave Slack to ship a PR comment or runbook step.
- **Generic webhook** — set `PILOTHOUSE_NOTIFY_WEBHOOK_URL` to fan
  approvals into Opsgenie, Discord, your own router etc.

Bulk operations:

```bash
pilothouse approvals approve-all --tool github_post_pr_comment --by alice
pilothouse approvals reject-all --agent scanner-foo --reason "rotate first"
```

Or `POST /approvals/resolve-batch` with an array of ids or filter spec.

### 8. Run lifecycle

| | |
|---|---|
| **Cancel** | `POST /runs/{id}/cancel` flips status; loop exits at the next iteration boundary. If paused at approval, all pending approvals are auto-rejected with the cancellation reason. |
| **Retry** | `POST /runs/{id}/retry` replays the same trigger payload as a fresh run. |
| **Search** | `GET /runs?status=&agent=&trigger=&q=&limit=&offset=` — tenant-wide cross-agent search with composable filters. |
| **JSON export** | `GET /runs/{id}/export.json` — full audit bundle (run + agent snapshot + every event + every approval). Drop into a SOC2 ticket. |
| **CSV export** | `GET /runs/{id}/export.csv` — event timeline flattened. |
| **Live SSE** | `GET /runs/{id}/events/stream` — replays history then attaches to the in-process bus, `event: end` closes when terminal. |
| **Pretty CLI** | `pilothouse runs logs <id>` — colored single-line-per-event timeline; `--kind tool_call --tail 20` to filter. |

### 9. Multi-tenancy

Single-tenant installs see no behaviour change — a `default` tenant is
bootstrapped automatically and the legacy `PILOTHOUSE_API_KEYS` env var
seeds its keys.

| | |
|---|---|
| **Isolation** | Every Agent / Run / Approval / MCP server carries `tenant_id`. Cross-tenant lookups return **404** (no information leak about existence). |
| **Auth** | `Authorization: Bearer <key>` or `X-API-Key: <key>` resolves to a tenant via the auth middleware; `request.state.tenant_id` is then enforced on every per-resource query. |
| **Same agent name across tenants** | Names are unique per-tenant via `(tenant_id, name)` constraint. |
| **Quotas** | `max_agents` (excess → 403) and `max_runs_per_day` (excess → 429) per tenant via `pilothouse tenants set-quota`. |
| **Rate limiting** | Sliding 60-second window per tenant via `PILOTHOUSE_RATE_LIMIT_PER_MINUTE`. |
| **Trigger dedup** | Webhook retries (Datadog/GitHub retry on 5xx) coalesce inside `PILOTHOUSE_DEDUP_WINDOW_SECONDS` — same agent + same payload digest returns the existing run id rather than starting another. |
| **Admin via CLI** | All tenant CRUD via `pilothouse tenants` — never via HTTP, so a compromised tenant key cannot escalate. |
| **Cascade delete** | `pilothouse tenants delete <name>` cleans up all owned agents/runs/approvals/MCP servers; the `default` tenant is protected. |
| **Masked key listing** | `pilothouse tenants show-keys <name>` shows `phk_xxxx…yyyy + length` — never plaintext after creation. |

```bash
pilothouse tenants create acme --display-name "Acme Corp"
pilothouse tenants add-key acme                  # auto-generates phk_… key
pilothouse tenants set-quota acme --max-agents 10 --max-runs-per-day 500
```

### 10. Git workflow conventions (bug-fix → PR)

`bug_auto_fixer` writes to your repo. To keep machine-authored history
recognisable, all PRs follow a strict, configurable convention:

| | Format | Example |
|---|---|---|
| **Branch** | `<prefix>/<type>/<TICKET-ID>-<slug>` | `pilothouse/fix/ENG-1234-npe-in-get-user-when-db-lookup-misses` |
| **Commit subject** | Conventional Commits `<type>(<scope>): <subject>` (≤ 72 chars) | `fix(users): NPE in get_user when DB lookup misses` |
| **Commit body** | optional plain-English context | "Adds an early return in `get_user` and a regression test." |
| **Commit footer** | `Closes <TICKET-ID>` (+ optional `Signed-off-by`) | `Closes ENG-1234` |
| **PR title** | same as commit subject | |
| **PR body** | summary + files touched + tests note + `Closes <TICKET-ID>` + `🤖 Opened by Pilothouse` footer | |

The `<prefix>` segment makes bot branches visually distinct from human
branches and lets repo admins write protection rules like:

```yaml
# .github/branch-protection.yml — illustrative
rules:
  - pattern: "pilothouse/**"
    required_reviewers: ["@sre"]
    require_signed_commits: true
```

Knobs (env or per-agent):

| Setting | Effect | Default |
|---|---|---|
| `PILOTHOUSE_GIT_BRANCH_PREFIX` / agent param `branch_prefix` | first segment of every bot branch | `pilothouse` |
| `PILOTHOUSE_GIT_COMMIT_SIGNOFF` | append `Signed-off-by:` trailer (DCO-friendly) | `false` |
| `PILOTHOUSE_GIT_PR_DRAFT` / agent param `draft` | open PRs as drafts so humans must mark ready | `false` |

The full pipeline (read ticket → read file → branch → commit → PR →
comment back to ticket) is one `bug_auto_fixer` run. Every write step
is **destructive** and gated:

```bash
# Run in dry-run first (default) — see exactly what it WOULD do.
pilothouse agents create bug-fixer bug_auto_fixer \
    --param repo='"acme/api"' --param label='"pilothouse-fix"'
echo '{"issue":{"identifier":"ENG-1234"}}' \
    | pilothouse agents trigger bug-fixer

# Flip to live + approval gates:
pilothouse agents create bug-fixer-live bug_auto_fixer \
    --param repo='"acme/api"' --param notify_slack_channel='"#sre-approvals"' \
    --no-dry-run
# → first destructive step (create_branch) pauses for approval; approving in
#   Slack auto-resumes through commit + PR + ticket comment.
```

### 11. PR code review (inline)

`pr_code_reviewer` is wired to GitHub PR webhooks. It posts a single
GitHub Review with:

- a top-level body summarising findings (verdict + counts per severity)
- inline comments anchored to specific `(path, line)` so each finding
  shows up next to the offending code in the PR diff view
- one of `APPROVE` / `REQUEST_CHANGES` / `COMMENT` based on severity

Dimensions are configurable per agent:

```bash
pilothouse agents create reviewer pr_code_reviewer \
    --param repo='"acme/api"' \
    --param dimensions='["correctness","security","tests"]' \
    --param block_on_findings=true
```

The webhook URL to register on the GitHub repo:
`POST /webhooks/github/<agent_id>` — verified via `X-Hub-Signature-256`.

### 12. GitOps (declarative agents)

Define your fleet in `agents.yaml`, commit to git, drive from CI:

```yaml
version: 1
defaults:
  dry_run: true
prune: false           # set true to delete agents missing from this file
agents:
  - name: triage-checkout
    template: datadog_alert_triage
    description: Investigate checkout latency alerts
    params:
      service: checkout
      slack_channel: "#sre-checkout"
      notify_slack_channel: "#sre-approvals"
  - name: nightly-flaky-scan
    template: flaky_test_hunter
    params:
      repo: acme/api
      tracking_issue: 42
    schedule_cron: "0 5 * * *"
```

```bash
pilothouse plan  -f agents.yaml                  # tf-style + ~ - diff
pilothouse apply -f agents.yaml --auto-approve   # CI-friendly
pilothouse export -o agents.yaml                 # round-trip current state
```

`POST /manifest/{plan,apply,export}` exposes the same workflow over HTTP.

### 13. Observability

| Surface | What it shows |
|---|---|
| **Audit log** | Every model turn, tool call, tool result, approval req/resolve/expire, decision, error → append-only `events` row. Replay reconstructs the run. |
| **Prometheus `/metrics`** | Counters: events by kind, tool invocations, approval decisions, run terminations. Gauges: agents, pending approvals, runs awaiting approval. |
| **`/stats?days=N`** | Aggregated: runs / tokens / cost-USD by day + by agent + by status. Backs the Console dashboard. |
| **SSE live tail** | `/runs/{id}/events/stream` for real-time UI updates without polling. |
| **Failure notifications** | `notify_on_failure: "#sre"` on an agent → Slack ping when a run reaches `failed` or `cancelled`; the same generic webhook fires too. |
| **Console dashboard** | Per-day cost bars, top-cost agents, run-status breakdown, `1d/7d/30d` window. |

### 14. Web console (Next.js 15)

Server-rendered, no client state. Eleven routes:

| Route | What |
|---|---|
| `/` | Top-line stats + recent runs across the tenant |
| `/dashboard` | Cost & token charts, by-day + by-agent + by-status |
| `/agents` | List with instant search (name / template / description) + template filter |
| `/agents/new` | Create form (template picker + JSON params editor) |
| `/agents/[id]` | Detail + manual trigger panel + recent runs |
| `/runs` | Tenant-wide search: status / agent / trigger / summary + pagination |
| `/runs/[id]` | Run summary + approvals + **live SSE timeline** + Cancel + Retry |
| `/approvals` | Pending / approved / rejected with **bulk select** approve/reject |
| `/schedule` | Cron-driven agents + computed next-fire times |
| `/system` | Templates + connectors with live/mock status |

The nav shows a **tenant indicator** pulled from `/me` so operators
always know which tenant they're acting on.

### 15. Deployment

| | |
|---|---|
| **Dockerfile** | Multi-stage, ~150 MB runtime image, non-root user, healthcheck on `/healthz`. |
| **Console Dockerfile** | Multi-stage Next.js build → minimal Node runtime. |
| **`docker-compose.yml`** | Brings up api + console + Postgres in one command; passes Anthropic key + secrets via env. |
| **Postgres-ready** | Just swap `PILOTHOUSE_DATABASE_URL` to `postgresql+asyncpg://…`; the compose stack does this automatically. |
| **CI workflow** | `.github/workflows/ci.yml` runs ruff + pytest (mock mode) + Next.js build + Docker image smoke test on every push. |

---

## Configuration

All settings are env vars prefixed `PILOTHOUSE_`. A `.env` file is
supported via pydantic-settings.

### Core

| var | purpose | default |
|---|---|---|
| `PILOTHOUSE_ANTHROPIC_API_KEY` | If set, runtime uses real Claude. Else mock. | `""` |
| `PILOTHOUSE_MODEL_PLANNER` | Model id for the planner | `claude-opus-4-5` |
| `PILOTHOUSE_MODEL_WORKER` | Model id for high-frequency small tasks | `claude-haiku-4-5` |
| `PILOTHOUSE_DATABASE_URL` | SQLAlchemy URL | sqlite under `./var` |
| `PILOTHOUSE_DATA_DIR` | Where SQLite + state files live | `./var` |
| `PILOTHOUSE_DRY_RUN_DEFAULT` | New agents created in dry-run by default | `true` |
| `PILOTHOUSE_HOST` / `PILOTHOUSE_PORT` | HTTP server bind | `127.0.0.1` / `8088` |
| `PILOTHOUSE_MAX_TOOL_ITERATIONS` | Cap per run; failed when exceeded | `12` |
| `PILOTHOUSE_MAX_OUTPUT_TOKENS` | Per-turn LLM output cap | `4096` |

### Auth + safety

| var | purpose | default |
|---|---|---|
| `PILOTHOUSE_REQUIRE_APPROVAL_FOR_WRITES` | Gate destructive tools on approval | `true` |
| `PILOTHOUSE_API_KEYS` | Legacy: comma-separated keys seeded into the default tenant on bootstrap | `""` |
| `PILOTHOUSE_APPROVAL_TTL_MINUTES` | Auto-reject pending approvals older than this | `60` |
| `PILOTHOUSE_APPROVAL_SWEEP_INTERVAL_SECONDS` | How often the sweeper runs | `30` |
| `PILOTHOUSE_DEDUP_WINDOW_SECONDS` | Trigger dedup window (0 to disable) | `60` |
| `PILOTHOUSE_RATE_LIMIT_PER_MINUTE` | Per-tenant trigger cap (0 to disable) | `60` |
| `PILOTHOUSE_METRICS_ENABLED` | Expose `/metrics` (Prometheus text) | `true` |
| `PILOTHOUSE_GIT_BRANCH_PREFIX` | First segment of auto-PR branches | `pilothouse` |
| `PILOTHOUSE_GIT_COMMIT_SIGNOFF` | Append `Signed-off-by:` to auto-commits | `false` |
| `PILOTHOUSE_GIT_PR_DRAFT` | Open auto-PRs as drafts | `false` |

### Connector credentials

| var | enables |
|---|---|
| `PILOTHOUSE_DATADOG_API_KEY` + `_APP_KEY` + `_SITE` | Live Datadog connector |
| `PILOTHOUSE_GITHUB_TOKEN` | Live GitHub connector |
| `PILOTHOUSE_PAGERDUTY_TOKEN` | Live PagerDuty connector |
| `PILOTHOUSE_SLACK_BOT_TOKEN` | Live Slack connector + Slack notifications |
| `PILOTHOUSE_KUBE_API_URL` + `_TOKEN` + `_CA_PATH` | Live Kubernetes connector |
| `PILOTHOUSE_LINEAR_API_KEY` | Live Linear connector (`bug_auto_fixer`) |

### Webhook secrets (one per source so you can rotate independently)

| var | scheme |
|---|---|
| `PILOTHOUSE_GITHUB_WEBHOOK_SECRET` | GitHub `X-Hub-Signature-256` |
| `PILOTHOUSE_SLACK_SIGNING_SECRET` | Slack v0 + 5-min window |
| `PILOTHOUSE_PAGERDUTY_WEBHOOK_SECRET` | PagerDuty multi-key `v1=…` |
| `PILOTHOUSE_DATADOG_WEBHOOK_SECRET` | Datadog `DD-Signature` |
| `PILOTHOUSE_WEBHOOK_SECRET` | Generic HMAC-SHA256 (alertmanager + generic) |

### Notifications

| var | purpose |
|---|---|
| `PILOTHOUSE_NOTIFY_WEBHOOK_URL` | Outbound URL invoked on `approval_requested` and `run_failure` |

---

## CLI reference

```
# server / DB
pilothouse serve                              # HTTP + scheduler + notifier
pilothouse db init                            # create tables, bootstrap default tenant

# discovery
pilothouse templates list
pilothouse connectors list
pilothouse connectors add-mcp NAME CMD ARG...        # stdio MCP server
pilothouse connectors add-mcp NAME --http URL \      # HTTP MCP server
    --header "Authorization=Bearer xxx" --destructive delete_user
pilothouse connectors remove-mcp NAME

# agents
pilothouse agents create NAME TEMPLATE \
       --param service=checkout \
       --param slack_channel='"#oncall"'      # JSON-parsed values
pilothouse agents list
pilothouse agents show <id-or-name>
pilothouse agents trigger <id> --file event.json
pilothouse agents delete <id>

# runs
pilothouse runs show <run-id>
pilothouse runs logs <run-id>                 # pretty colored timeline
pilothouse runs cancel <run-id> --by alice
pilothouse runs retry <run-id>

# approvals
pilothouse approvals list                     # pending (default)
pilothouse approvals show <approval-id>
pilothouse approvals approve <approval-id> --by alice
pilothouse approvals reject <approval-id> --by alice --reason "rotate first"
pilothouse approvals approve-all --tool github_post_pr_comment --by alice
pilothouse approvals reject-all --agent scanner-foo --by alice --reason "..."
pilothouse sweep-approvals                    # one-shot TTL sweep

# tenants
pilothouse tenants list
pilothouse tenants create acme --display-name "Acme Corp"
pilothouse tenants add-key acme               # auto-generates phk_… key
pilothouse tenants add-key acme --key existing-key
pilothouse tenants remove-key acme <key>
pilothouse tenants set-quota acme --max-agents 10 --max-runs-per-day 500
pilothouse tenants show-keys acme             # masked
pilothouse tenants delete acme

# GitOps
pilothouse plan -f agents.yaml
pilothouse apply -f agents.yaml --auto-approve
pilothouse export -o agents.yaml

# misc
pilothouse demo                               # bootstrap one of each + run
```

---

## HTTP API

OpenAPI auto-docs at `/docs`, ReDoc at `/redoc`. Endpoints are tagged
in groups: `meta`, `agents`, `runs`, `approvals`, `manifest`, `stats`,
`schedule`, `webhooks`, `metrics`.

### meta

| method | path | notes |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/me` | resolved tenant for the inbound API key |
| GET | `/templates` | list templates |
| GET | `/connectors` | list connectors + live/mock status |

### agents

| method | path | notes |
|---|---|---|
| POST | `/agents` | create |
| GET | `/agents` | list |
| GET | `/agents/{id}` | get |
| PATCH | `/agents/{id}` | update |
| DELETE | `/agents/{id}` | delete |
| POST | `/agents/{id}/trigger` | manual run, returns the new Run |
| GET | `/agents/{id}/runs` | recent runs |

### runs

| method | path | notes |
|---|---|---|
| GET | `/runs` | tenant-wide search: `?status=&agent=&trigger=&q=&limit=&offset=` |
| GET | `/runs/{id}` | run summary |
| GET | `/runs/{id}/events` | full audit log |
| GET | `/runs/{id}/events/stream` | SSE: replay then live (`event: end` closes) |
| GET | `/runs/{id}/export.json` | full audit bundle (run + agent + events + approvals) |
| GET | `/runs/{id}/export.csv` | event timeline as CSV |
| POST | `/runs/{id}/cancel` | `{"by":"…"}` — cooperative cancellation |
| POST | `/runs/{id}/retry` | re-execute with the same trigger payload |
| GET | `/runs/{id}/approvals` | approvals attached to a run |

### approvals

| method | path | notes |
|---|---|---|
| GET | `/approvals` | filter `?status=&tool=&agent=` |
| GET | `/approvals/{id}` | get one |
| POST | `/approvals/{id}/resolve` | `{"decision":"approve\|reject", "resolved_by", "reason"}` — auto-resumes when last approval clears |
| POST | `/approvals/resolve-batch` | bulk by ids array OR by `filters: {tool?, agent?}` |

### manifest, stats, schedule, metrics

| method | path | notes |
|---|---|---|
| POST | `/manifest/plan` | compute diff against supplied manifest |
| POST | `/manifest/apply` | plan + persist |
| GET | `/manifest/export` | dump current state as a manifest |
| GET | `/stats?days=N` | aggregated runs / tokens / cost |
| GET | `/schedule` | scheduled agents + next-fire timestamps |
| GET | `/metrics` | Prometheus text |

### webhooks

| method | path | notes |
|---|---|---|
| POST | `/webhooks/datadog/{agent_id}` | source-verified |
| POST | `/webhooks/github/{agent_id}` | `X-Hub-Signature-256` |
| POST | `/webhooks/pagerduty/{agent_id}` | multi-key `v1=…` |
| POST | `/webhooks/slack/{agent_id}` | Slack v0 |
| POST | `/webhooks/alertmanager/{agent_id}` | generic HMAC |
| POST | `/webhooks/generic/{agent_id}` | generic HMAC |
| POST | `/webhooks/slack/interactivity` | Slack interactive component callback (Approve / Reject buttons) |

---

## Tests

```bash
uv sync --all-extras        # or `pip install -e ".[dev]"`
uv run pytest -q            # or just `pytest -q` inside an activated venv
```

The suite (150+ tests) runs entirely in mock mode — no Anthropic key,
no external HTTP. CI runs the same on every push.

---

## Execution backends

Pilothouse picks one of three executors automatically from
`PILOTHOUSE_TEMPORAL_ADDRESS` — the public orchestration API
(`execute_agent`, `resume_run`, `cancel_run`, `retry_run`) is identical
across all three:

| Mode | env value | What you get |
|---|---|---|
| **in-process** | _unset_ (default) | Asyncio in the same process. Zero external infra. |
| **Temporal dev** | `dev` | In-process Temporal dev server — **durable workflows on a single machine** without running a Temporal cluster. |
| **Temporal cluster** | `host:7233` | Connects to a real Temporal cluster. Workflows survive process restarts; workers scale horizontally. |

Temporal mode wraps each Run as a `PilothouseAgentRun` workflow:

```
client.start_workflow(PilothouseAgentRun.run, payload)
  → run_agent_activity      # delegates to AgentRunner.start
  → wait_condition          # parks workflow until approval_resolved signal
  → resume_run_activity     # delegates to AgentRunner.resume
  → return run_id
```

Cancellation routes through workflow signals; approvals through
`approval_resolved` signals; nothing else in the system (templates,
connectors, plugins, console, CLI) is aware Temporal is involved.

`temporalio` is an **optional dependency**:

```bash
# uv
uv sync                                      # in-process only
uv sync --extra temporal                     # dev / cluster mode available

# pip
pip install pilothouse                       # in-process only
pip install 'pilothouse[temporal]'           # dev / cluster mode available
```

Inspect the active mode with `pilothouse temporal status`.

## Roadmap (not yet implemented)

- **RBAC inside a tenant** — currently flat (any tenant key can do
  anything within the tenant).
- **SSO / OAuth2 / SAML** for the console.
- **Sandboxed code execution** for IaC plan/diff agents that need to
  run terraform/opentofu locally.
- **Conversation memory across runs** — long-running incident triage
  agents that remember "I already looked at this last hour."
- **Helm chart** for Kubernetes deployment.
- **Python + TypeScript SDKs** as separate published packages.

---

## License

Apache-2.0.
