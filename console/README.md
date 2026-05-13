# Pilothouse Console

Next.js 15 + Tailwind UI for the Pilothouse FastAPI server.

## Run it

```bash
# 1. Start the Pilothouse API in another terminal:
pilothouse serve              # http://127.0.0.1:8088

# 2. Install console deps and run dev server:
cd console
npm install
npm run dev                   # http://localhost:3000
```

The console reads the API URL from `PILOTHOUSE_API` (default
`http://127.0.0.1:8088`). All data is fetched on every request — no client
state, no cache — so the UI always reflects the source of truth.

## Pages

- `/` — dashboard: counts + recent runs
- `/agents` — list agents
- `/agents/new` — create an agent (picks a template, edits params JSON)
- `/agents/[id]` — agent detail + manual trigger panel + recent runs
- `/runs/[id]` — run summary + approvals + event timeline
- `/approvals?status=pending` — pending / approved / rejected approvals, with
  one-click approve / reject that auto-resumes the run
- `/system` — registered templates and connectors with live/mock state
