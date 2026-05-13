# Pilothouse User Guide

**English** · [简体中文](./USER_GUIDE.zh-CN.md)

A complete walkthrough for **operators** — running Pilothouse,
configuring agents, triggering them, approving destructive actions,
and managing tenants. If you want to write a plugin, see
[PLUGIN_AUTHORING.md](./PLUGIN_AUTHORING.md) instead.

## 1. What Pilothouse is, in one paragraph

Pilothouse is a server that runs **LLM-powered agents** on triggers.
You register an **Agent** — a small config: a name, a *template*
(what playbook to run), parameters, optional cron schedule. From then
on, when something happens (cron fires / a webhook arrives / you
trigger manually), the agent's template tells an LLM what to do, the
LLM calls **tools** from registered **connectors** (Datadog,
GitHub, …), and Pilothouse records every step in an append-only audit
log. Anything destructive (post a comment, delete a thing) is gated
either by **dry-run** (preview only) or by **approval** (human
decides). Everything is multi-tenant from day one.

## 2. Installing

### Local Python with `uv` (recommended)

[`uv`](https://docs.astral.sh/uv/) is the fastest path: it provisions
the right Python interpreter, manages the venv, and produces a
reproducible `uv.lock`. The repo pins `3.12` via `.python-version`, so
the first sync downloads a matching CPython automatically.

```bash
# 1. Install uv (once per machine)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone + sync all extras (dev + temporal)
git clone <repo>
cd Pilothouse
uv sync --all-extras
```

Run everything through `uv run` — no manual `activate` needed:

```bash
uv run pilothouse db init        # initialise SQLite + default tenant
uv run pilothouse demo           # mock-mode end-to-end demo (no API key)
uv run pytest -q                 # the test suite (150+ tests)
```

Common follow-ups:

```bash
uv add httpx                     # add a runtime dep (updates uv.lock)
uv add --group dev pytest-xdist  # add a dev-only dep
uv sync                          # reproduce the locked env exactly
uv lock --upgrade                # bump all deps within pyproject ranges
```

### Local Python with `pip` (traditional)

If you'd rather use vanilla pip / venv:

```bash
git clone <repo>
cd Pilothouse
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .             # in-process executor only
# OR
pip install -e '.[temporal]' # also enables Temporal mode (durable workflows)

pilothouse db init
pilothouse demo
```

### Day-to-day: `uv` vs `pip`

| Task | uv | pip / venv |
|---|---|---|
| Activate env | (not needed — use `uv run …`) | `source .venv/bin/activate` |
| Run a command | `uv run pilothouse …` | `pilothouse …` (inside venv) |
| Run tests | `uv run pytest -q` | `pytest -q` |
| Add runtime dep | `uv add foo` | edit `pyproject.toml` + `pip install -e .` |
| Add dev dep | `uv add --group dev foo` | edit `pyproject.toml` + reinstall |
| Upgrade everything | `uv lock --upgrade && uv sync` | `pip install -U -e '.[dev,temporal]'` |
| Switch Python | edit `.python-version`, `uv sync` | recreate venv with new interpreter |
| Reproducible install | `uv sync` (uses `uv.lock`) | `pip install -e .` (no lock) |

`uv.lock` is committed — every contributor and every CI run gets the
same dependency tree.

### Docker (api + console + Postgres)

```bash
docker compose up
# api      http://localhost:8088
# console  http://localhost:3000
```

The compose stack wires Postgres as the database and exposes both the
API and the console. Override `PILOTHOUSE_ANTHROPIC_API_KEY` and any
connector credentials via your shell environment before `up`.

## 3. The mental model

Three nouns matter:

| | What it is | Example |
|---|---|---|
| **Template** | A playbook the agent follows. Defines the system prompt, allowed tools, and a deterministic `mock_plan` so you can run it without LLM credits. Ships as code. | `datadog_alert_triage`, `bug_auto_fixer` |
| **Connector** | A namespaced bundle of *tools* the LLM can call. Tools are either read-only or destructive. Ships as code or as an MCP server. | `github`, `datadog`, `linear` |
| **Agent** | An instance of one template, with your parameters + your trigger. The only thing operators create. | "Investigate `checkout` alerts and post to `#sre`" |

Every **Run** is one execution of one agent. Runs have a status
(`running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`),
an event log, and (optionally) attached approvals.

## 4. Your first agent

There are three ways to create agents. Pick the one that fits your
workflow.

### 4.1 CLI

```bash
pilothouse agents create checkout-triage datadog_alert_triage \
    --param service=checkout \
    --param 'slack_channel="#sre-checkout"' \
    --no-dry-run                               # destructive tools will gate on approval
```

Inspect it:

```bash
pilothouse agents show checkout-triage
pilothouse agents list
```

Trigger it manually:

```bash
echo '{"alert_id":"abc-123","service":"checkout"}' \
    | pilothouse agents trigger checkout-triage
```

### 4.2 HTTP API

```bash
curl -X POST http://127.0.0.1:8088/agents \
    -H 'content-type: application/json' \
    -d '{"name":"checkout-triage",
         "template":"datadog_alert_triage",
         "params":{"service":"checkout","slack_channel":"#sre-checkout"},
         "dry_run":false}'
```

Trigger:

```bash
curl -X POST http://127.0.0.1:8088/agents/<id>/trigger \
    -d '{"payload":{"alert_id":"abc-123"}}'
```

### 4.3 GitOps — `agents.yaml`

The recommended path for production. Commit `agents.yaml` to git, let
CI apply it. Plans are tf/kubectl-style: `+` add, `~` change, `-`
delete.

```yaml
# agents.yaml
version: 1
defaults:
  dry_run: true
prune: false                              # set true to delete agents not in this file
agents:
  - name: checkout-triage
    template: datadog_alert_triage
    description: Investigate checkout latency alerts
    params:
      service: checkout
      slack_channel: "#sre-checkout"
      notify_slack_channel: "#sre-approvals"     # approval routing
  - name: nightly-flaky-scan
    template: flaky_test_hunter
    params:
      repo: acme/api
      tracking_issue: 42
    schedule_cron: "0 5 * * *"
```

```bash
pilothouse plan  -f agents.yaml                      # preview
pilothouse apply -f agents.yaml --auto-approve       # CI-friendly
pilothouse export -o agents.yaml                     # round-trip from current state
```

The same workflow is also available over HTTP at `/manifest/{plan,apply,export}`.

## 5. Triggering agents

| How | When to use |
|---|---|
| **Manual (CLI)** | `pilothouse agents trigger <id> --file event.json` — for testing, one-off runs |
| **Manual (HTTP)** | `POST /agents/{id}/trigger` — for scripts / dashboards |
| **Cron** | Set `schedule_cron` on the agent; APScheduler fires it. Inspect with `pilothouse plugins list` (built-in scheduler is exposed via `/schedule`). |
| **Webhook (Datadog)** | `POST /webhooks/datadog/{agent_id}` — verified via `DD-Signature` |
| **Webhook (GitHub)** | `POST /webhooks/github/{agent_id}` — verified via `X-Hub-Signature-256` |
| **Webhook (PagerDuty)** | `POST /webhooks/pagerduty/{agent_id}` — verified via `X-PagerDuty-Signature` (multi-key for rotation) |
| **Webhook (Slack)** | `POST /webhooks/slack/{agent_id}` — Slack v0 + 5-minute window |
| **Webhook (Alertmanager / generic)** | `POST /webhooks/{alertmanager,generic}/{agent_id}` — generic HMAC |
| **Custom (plugin)** | A `TriggerPlugin` you write — Kafka consumer, file watcher, anything |

Each webhook source has its own secret env var
(`PILOTHOUSE_<SOURCE>_WEBHOOK_SECRET`) so keys rotate independently.
An empty secret skips verification (dev-friendly default).

### Deduplication

Webhook retries (Datadog/GitHub will retry on 5xx) coalesce inside
`PILOTHOUSE_DEDUP_WINDOW_SECONDS` (default 60s). Same agent + same
payload digest returns the existing run id rather than starting another.

### Rate limiting

Per-tenant sliding 60-second window via `PILOTHOUSE_RATE_LIMIT_PER_MINUTE`
(default 60). Excess triggers return HTTP 429.

## 6. The approval workflow

By default every destructive tool is gated. Two layers:

```
trigger → agent → tool call → is it destructive?
                                  │
                       ┌──────────┴──────────┐
                       │                     │
                 dry_run=true           dry_run=false
                       │                     │
                "would_have_called    require_approval_for_writes?
                 with: …" preview            │
                       │              ┌──────┴──────┐
                       │              │             │
                       │           true (default)  false
                       │              │             │
                       │       pause + Approval   execute
                       │       row created
                       │              │
                       │       await human decision
                       │              │
                       └──────────────┴── run completes
```

### Reviewing pending approvals

```bash
pilothouse approvals list                       # default: pending only
pilothouse approvals show <approval-id>         # full payload + assistant rationale
```

Or use the console: `/approvals` page with bulk select.

### Approving

```bash
pilothouse approvals approve <approval-id> --by alice
pilothouse approvals reject <approval-id> --by alice --reason "rotate key first"

# Bulk:
pilothouse approvals approve-all --tool github_post_pr_comment --by alice
pilothouse approvals reject-all --agent scanner-foo --reason "PR author should fix"
```

The run resumes automatically once the last approval is resolved.

### Slack-native approval

Set `notify_slack_channel: "#sre"` on an agent. When that agent
requests approval, a Slack message with Approve / Reject buttons
appears in the channel. Clicking POSTs back to a signed webhook,
resolves the approval, and updates the message in-place. **No need
to leave Slack** to ship a PR comment or a runbook step.

Setup:

1. Install your Slack bot in the workspace; give it `chat.write` +
   `commands` scopes.
2. `export PILOTHOUSE_SLACK_BOT_TOKEN=xoxb-…`
3. Create a Slack app **Interactivity** subscription pointing to
   `https://<pilothouse-host>/webhooks/slack/interactivity`.
4. `export PILOTHOUSE_SLACK_SIGNING_SECRET=…` so Pilothouse verifies
   the v0 signature.

### Approval TTL

Pending approvals older than `PILOTHOUSE_APPROVAL_TTL_MINUTES`
(default 60) are auto-rejected by a background sweeper. The run
resumes with a structured "expired" rejection result fed to the LLM,
so the model can decide what to do (typically: log the expiry and
exit). **Nothing dangles forever**.

## 7. Monitoring runs

### Run search

```bash
# CLI
pilothouse runs show <run-id>
pilothouse runs logs <run-id>            # pretty colored timeline
pilothouse runs logs <run-id> --kind tool_call --tail 20

# HTTP — tenant-wide search
curl 'http://127.0.0.1:8088/runs?status=succeeded&agent=checkout-triage&limit=20'
```

### Live event stream (SSE)

```
GET /runs/{id}/events/stream
```

Replays the run's full event history, then attaches to the in-process
bus for live events. Closes with `event: end` once the run reaches a
terminal state. The console's run-detail page uses this for the
auto-scrolling timeline.

### Audit export

```
GET /runs/{id}/export.json       # full audit bundle (run + agent + events + approvals)
GET /runs/{id}/export.csv        # event timeline as CSV
```

Drop into a SOC2 ticket and you have a complete answer to "what did
the AI do, when, with whose approval".

### Lifecycle controls

```bash
pilothouse runs cancel <run-id> --by alice          # mid-run cancellation
pilothouse runs retry <run-id>                      # replay with same payload
```

Cancellation is cooperative — the runtime exits at the next loop
boundary. If the run is paused at approval, all pending approvals get
rejected with a "Run cancelled" reason.

### Dashboard + metrics

- **`/dashboard`** in the console — cost per day, top agents by cost,
  run-status breakdown, configurable window (1d/7d/30d).
- **`GET /stats?days=N`** — same data as JSON for your own dashboards.
- **`GET /metrics`** — Prometheus exposition format. Counters for
  events/tools/approvals/run terminations, gauges for agents +
  pending approvals + paused runs.

## 8. Multi-tenancy

Single-tenant installs see no behaviour change — a `default` tenant
is bootstrapped automatically, and the legacy
`PILOTHOUSE_API_KEYS` env var seeds its API keys.

To carve out additional tenants:

```bash
pilothouse tenants create acme --display-name "Acme Corp"
pilothouse tenants add-key acme                    # auto-generates a `phk_…` key (printed once)
pilothouse tenants add-key acme --key existing-key
pilothouse tenants set-quota acme \
    --max-agents 10 \
    --max-runs-per-day 500
pilothouse tenants show-keys acme                  # masked listing
```

Console / external clients then authenticate with one of:

```
Authorization: Bearer <key>
X-API-Key: <key>
```

Cross-tenant lookups return **404** (not 403) — Pilothouse refuses to
disclose whether an id exists in another tenant.

### Tenant admin model

All tenant CRUD goes through `pilothouse tenants` — *never* through
HTTP. This means a compromised tenant key cannot escalate to other
tenants or create new ones; only an operator with shell access to the
Pilothouse host can.

## 9. Configuration reference

All settings are env vars prefixed `PILOTHOUSE_`. A `.env` file is
auto-loaded.

### Core

| Var | Default | Purpose |
|---|---|---|
| `PILOTHOUSE_ANTHROPIC_API_KEY` | `""` | If set, runtime uses real Claude. Else mock mode. |
| `PILOTHOUSE_MODEL_PLANNER` | `claude-opus-4-5` | Model id for the planner |
| `PILOTHOUSE_MODEL_WORKER` | `claude-haiku-4-5` | Model id for high-frequency small tasks |
| `PILOTHOUSE_DATABASE_URL` | sqlite under `./var` | SQLAlchemy URL |
| `PILOTHOUSE_HOST` / `PILOTHOUSE_PORT` | `127.0.0.1` / `8088` | HTTP server bind |

### Auth + safety

| Var | Default | Purpose |
|---|---|---|
| `PILOTHOUSE_REQUIRE_APPROVAL_FOR_WRITES` | `true` | Gate destructive tools on approval |
| `PILOTHOUSE_API_KEYS` | `""` | Legacy: comma-separated keys seeded into the default tenant |
| `PILOTHOUSE_APPROVAL_TTL_MINUTES` | `60` | Auto-reject stale pending approvals |
| `PILOTHOUSE_DEDUP_WINDOW_SECONDS` | `60` | Trigger deduplication window |
| `PILOTHOUSE_RATE_LIMIT_PER_MINUTE` | `60` | Per-tenant trigger cap |

### Executor

| Var | Default | Purpose |
|---|---|---|
| `PILOTHOUSE_TEMPORAL_ADDRESS` | `""` | `""` = in-process; `dev` = embedded Temporal server; `host:7233` = cluster |
| `PILOTHOUSE_TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `PILOTHOUSE_TEMPORAL_TASK_QUEUE` | `pilothouse` | Worker task queue |

### Connector credentials

| Var | Enables |
|---|---|
| `PILOTHOUSE_DATADOG_API_KEY` + `_APP_KEY` + `_SITE` | Live Datadog connector |
| `PILOTHOUSE_GITHUB_TOKEN` | Live GitHub connector |
| `PILOTHOUSE_PAGERDUTY_TOKEN` | Live PagerDuty connector |
| `PILOTHOUSE_SLACK_BOT_TOKEN` | Live Slack connector + Slack notifications |
| `PILOTHOUSE_KUBE_API_URL` + `_TOKEN` + `_CA_PATH` | Live Kubernetes connector |
| `PILOTHOUSE_LINEAR_API_KEY` | Live Linear connector |

### Webhook secrets (per-source rotation)

| Var | Scheme |
|---|---|
| `PILOTHOUSE_GITHUB_WEBHOOK_SECRET` | GitHub `X-Hub-Signature-256` |
| `PILOTHOUSE_SLACK_SIGNING_SECRET` | Slack v0 + 5-min window |
| `PILOTHOUSE_PAGERDUTY_WEBHOOK_SECRET` | PagerDuty multi-key `v1=…` |
| `PILOTHOUSE_DATADOG_WEBHOOK_SECRET` | Datadog `DD-Signature` |
| `PILOTHOUSE_WEBHOOK_SECRET` | Generic HMAC-SHA256 |

### Notifications

| Var | Purpose |
|---|---|
| `PILOTHOUSE_NOTIFY_WEBHOOK_URL` | Outbound URL fired on `approval_requested` + `run_failure` |

### Git workflow (auto-PR templates)

| Var | Default | Purpose |
|---|---|---|
| `PILOTHOUSE_GIT_BRANCH_PREFIX` | `pilothouse` | First segment of bot branch names |
| `PILOTHOUSE_GIT_COMMIT_SIGNOFF` | `false` | Append `Signed-off-by:` trailer |
| `PILOTHOUSE_GIT_PR_DRAFT` | `false` | Open auto-PRs as drafts |

## 10. Deployment

### Option A — single process, SQLite (smallest)

```bash
pilothouse serve     # listens on 127.0.0.1:8088
```

Good for development and small single-tenant deployments. No external
services needed. Storage in `./var/pilothouse.db`.

### Option B — Docker compose (api + console + Postgres)

```bash
docker compose up -d
```

Three containers wired together. Override the Anthropic key + any
connector tokens via env. Database persists in a Docker volume.

### Option C — Postgres + Temporal (production)

```bash
export PILOTHOUSE_DATABASE_URL='postgresql+asyncpg://user:pw@db:5432/pilothouse'
export PILOTHOUSE_TEMPORAL_ADDRESS=temporal.svc.cluster.local:7233
export PILOTHOUSE_API_KEYS=your-admin-key

pip install 'pilothouse[temporal]'
pilothouse serve
```

Workflows are now durable across restarts; multiple worker processes
can run against the same Temporal cluster for horizontal scaling.
Verify with `pilothouse temporal status`.

### Health checks

| Path | What |
|---|---|
| `GET /healthz` | Liveness — returns `{"ok":true}` |
| `GET /metrics` | Prometheus scrape target |
| `GET /plugins/doctor` | Returns any plugin currently misconfigured |
| `pilothouse plugins doctor` | Same, exits non-zero — handy as a CI gate |

## 11. Plugins (one minute)

Plugins are how Pilothouse is extended. There are five kinds —
**Template**, **Connector**, **Notifier**, **Trigger**, **Hook** — and
three discovery paths:

| Path | How |
|---|---|
| **Built-in** | Ships with the wheel (`pilothouse.plugins.builtin`) |
| **Entry point** | `pip install pilothouse-foo` (declares `pilothouse.plugins` entry point) |
| **Directory** | Drop `*.py` into `$PILOTHOUSE_PLUGIN_DIR` |

CLI:

```bash
pilothouse plugins list                              # all discovered plugins
pilothouse plugins info <name>                       # one-plugin detail
pilothouse plugins enable  / disable <name>          # persists
pilothouse plugins config set <name> <key> <value>
pilothouse plugins doctor                            # CI gate: exits non-zero on misconfig
pilothouse plugins install <package>                 # pip install + auto-reload
pilothouse plugins scaffold <kind> <name>            # 30-second author start
```

**Authoring a plugin?** See [PLUGIN_AUTHORING.md](./PLUGIN_AUTHORING.md).

## 12. FAQ + gotchas

**Q: Do I need an Anthropic API key to try this out?**
No. With `PILOTHOUSE_ANTHROPIC_API_KEY` empty, every template runs in
*mock mode*: deterministic replay of `mock_plan` exercising the same
tools the real model would call. Useful for demos, CI, and connector
development.

**Q: How do I see what an agent is *about to* do without it doing anything?**
Create the agent in dry-run (the default), trigger it, then look at
the run's event log. Every destructive tool produces a
`would_have_called_with: {…}` preview instead of executing.

**Q: An agent triggered 50 times during an alert storm. What happened?**
Check `PILOTHOUSE_DEDUP_WINDOW_SECONDS` — webhook retries should
coalesce. If you're getting 50 distinct payloads, the source is
firing 50 distinct alerts; raise `PILOTHOUSE_RATE_LIMIT_PER_MINUTE`
or look at the upstream alert config.

**Q: Where does an approval go if no one's watching?**
It expires after `PILOTHOUSE_APPROVAL_TTL_MINUTES` (default 60).
The agent gets a structured "rejected: expired" result and continues
(usually exits cleanly). Set `notify_slack_channel` on the agent (or
`PILOTHOUSE_NOTIFY_WEBHOOK_URL` globally) to make sure someone sees it.

**Q: I want a tool the agent shouldn't be able to call.**
Two options:
1. Don't expose it — write a connector that only exposes the read
   path.
2. Expose it but flag `is_destructive=True` — every call goes
   through dry-run + approval, and you control whether to approve.

**Q: How do I run this without giving it any cloud credentials?**
Don't set any `PILOTHOUSE_*_TOKEN` / `_API_KEY` env vars. Connectors
fall back to **mock mode** with deterministic synthetic data — you
can drive every template end-to-end without touching a real service.

**Q: Can I run multiple Pilothouse processes against the same database?**
Yes, if your `PILOTHOUSE_DATABASE_URL` is Postgres. SQLite isn't
recommended for multi-process; the dedup / rate-limit state is also
per-process (use Temporal mode for cross-process workflow durability).

**Q: A plugin is broken and I can't disable it via the CLI.**
The plugins table accepts a raw `UPDATE`:
```sql
UPDATE plugins SET enabled = false WHERE name = 'broken_plugin';
```
Or remove the file from `PILOTHOUSE_PLUGIN_DIR` if it's directory-discovered.

## Next steps

- **Writing a plugin** → [PLUGIN_AUTHORING.md](./PLUGIN_AUTHORING.md)
- **Example plugins** → [`examples/plugins/`](../examples/plugins/)
- **Bilingual project README** → [`../README.md`](../README.md)
