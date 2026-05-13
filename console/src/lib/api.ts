/** Thin client for the Pilothouse FastAPI server.
 *
 * Every page in `app/` reaches Pilothouse only through these functions —
 * no direct `fetch` calls scattered through components. Server components
 * call these without caching so the console always reflects current state.
 */

const BASE = process.env.PILOTHOUSE_API ?? "http://127.0.0.1:8088";

export type AgentOut = {
  id: string;
  name: string;
  template: string;
  description: string;
  params: Record<string, unknown>;
  schedule_cron: string | null;
  enabled: boolean;
  dry_run: boolean;
  created_at: string;
  updated_at: string;
};

export type RunOut = {
  id: string;
  agent_id: string;
  trigger: string;
  status: string;
  summary: string;
  tokens_input: number;
  tokens_output: number;
  cost_usd_cents: number;
  started_at: string;
  finished_at: string | null;
};

export type EventOut = {
  id: string;
  kind: string;
  data: Record<string, unknown>;
  created_at: string;
};

export type ApprovalOut = {
  id: string;
  run_id: string;
  tool_name: string;
  tool_use_id: string;
  tool_input: Record<string, unknown>;
  rationale: string;
  status: string;
  resolved_by: string | null;
  rejection_reason: string;
  created_at: string;
  resolved_at: string | null;
};

export type TemplateOut = {
  key: string;
  name: string;
  description: string;
  default_tools: string[];
};

export type ConnectorOut = {
  name: string;
  live: boolean;
  tools: { name: string; destructive: boolean }[];
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${init?.method ?? "GET"} ${path} → ${res.status} ${body}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export type Me = {
  tenant_id: string;
  tenant_name: string;
  tenant_display_name: string;
};

export type Stats = {
  window_days: number;
  totals: { runs: number; tokens_in: number; tokens_out: number; cost_usd: number };
  by_day: { date: string; runs: number; tokens_in: number; tokens_out: number; cost_usd: number }[];
  by_agent: { agent: string; runs: number; tokens_in: number; tokens_out: number; cost_usd: number }[];
  by_status: Record<string, number>;
};

export const api = {
  me: () => req<Me>("/me"),
  stats: (days = 7) => req<Stats>(`/stats?days=${days}`),
  searchRuns: (params: {
    status?: string;
    agent?: string;
    trigger?: string;
    q?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const sp = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
    });
    const qs = sp.toString();
    return req<RunOut[]>(`/runs${qs ? `?${qs}` : ""}`);
  },
  agents: () => req<AgentOut[]>("/agents"),
  agent: (id: string) => req<AgentOut>(`/agents/${id}`),
  agentRuns: (id: string) => req<RunOut[]>(`/agents/${id}/runs`),
  run: (id: string) => req<RunOut>(`/runs/${id}`),
  runEvents: (id: string) => req<EventOut[]>(`/runs/${id}/events`),
  runApprovals: (id: string) => req<ApprovalOut[]>(`/runs/${id}/approvals`),
  approvals: (status?: string) =>
    req<ApprovalOut[]>(`/approvals${status ? `?status=${status}` : ""}`),
  approval: (id: string) => req<ApprovalOut>(`/approvals/${id}`),
  resolveApproval: (id: string, decision: "approve" | "reject", resolved_by = "console", reason = "") =>
    req<RunOut>(`/approvals/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ decision, resolved_by, reason }),
    }),
  resolveApprovalBatch: (
    ids: string[],
    decision: "approve" | "reject",
    resolved_by = "console",
    reason = "",
  ) =>
    req<{ resolved: { id: string; ok: boolean; error?: string }[]; count: number }>(
      "/approvals/resolve-batch",
      {
        method: "POST",
        body: JSON.stringify({ ids, decision, resolved_by, reason }),
      },
    ),
  triggerAgent: (id: string, payload: Record<string, unknown>, dry_run: boolean | null = null) =>
    req<RunOut>(`/agents/${id}/trigger`, {
      method: "POST",
      body: JSON.stringify({ payload, dry_run }),
    }),
  retryRun: (id: string, dry_run: boolean | null = null) =>
    req<RunOut>(`/runs/${id}/retry`, {
      method: "POST",
      body: JSON.stringify(dry_run === null ? {} : { dry_run }),
    }),
  cancelRun: (id: string, by = "console") =>
    req<RunOut>(`/runs/${id}/cancel`, { method: "POST", body: JSON.stringify({ by }) }),
  createAgent: (input: {
    name: string;
    template: string;
    description?: string;
    params?: Record<string, unknown>;
    schedule_cron?: string | null;
    enabled?: boolean;
    dry_run?: boolean;
  }) =>
    req<AgentOut>("/agents", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  deleteAgent: (id: string) => req<void>(`/agents/${id}`, { method: "DELETE" }),
  templates: () => req<TemplateOut[]>("/templates"),
  connectors: () => req<ConnectorOut[]>("/connectors"),
  plugins: () =>
    req<
      {
        name: string;
        version: string;
        description: string;
        kinds: string[];
        source: string;
        enabled: boolean;
        misconfig_reason: string;
        config_schema: {
          name: string;
          description: string;
          required: boolean;
          secret: boolean;
          default: string;
          env_fallback: string;
        }[];
      }[]
    >("/plugins"),
  pluginConfig: (name: string) =>
    req<
      Record<
        string,
        { value: string; source: string; secret: boolean; required: boolean; description: string }
      >
    >(`/plugins/${encodeURIComponent(name)}/config`),
  pluginEnable: (name: string) =>
    req<{ name: string; enabled: boolean }>(`/plugins/${encodeURIComponent(name)}/enable`, {
      method: "POST",
    }),
  pluginDisable: (name: string) =>
    req<{ name: string; enabled: boolean }>(`/plugins/${encodeURIComponent(name)}/disable`, {
      method: "POST",
    }),
  pluginSetConfig: (name: string, key: string, value: string) =>
    req(`/plugins/${encodeURIComponent(name)}/config`, {
      method: "POST",
      body: JSON.stringify({ key, value }),
    }),
  pluginUnsetConfig: (name: string, key: string) =>
    req(`/plugins/${encodeURIComponent(name)}/config`, {
      method: "POST",
      body: JSON.stringify({ unset: key }),
    }),
  pluginReload: () => req<{ count: number }>("/plugins/reload", { method: "POST" }),
  schedule: () =>
    req<{
      id: string;
      name: string;
      template: string;
      cron: string;
      next_fire: string | null;
      dry_run: boolean;
    }[]>("/schedule"),
};

export function classifyStatus(status: string): "ok" | "warn" | "err" | "info" | "neutral" {
  switch (status) {
    case "succeeded":
    case "approved":
      return "ok";
    case "running":
    case "pending":
    case "awaiting_approval":
      return "warn";
    case "failed":
    case "rejected":
      return "err";
    case "cancelled":
      return "neutral";
    default:
      return "info";
  }
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function relTime(iso: string): string {
  const t = new Date(iso).getTime();
  const dt = Date.now() - t;
  if (dt < 60_000) return `${Math.floor(dt / 1000)}s ago`;
  if (dt < 3_600_000) return `${Math.floor(dt / 60_000)}m ago`;
  if (dt < 86_400_000) return `${Math.floor(dt / 3_600_000)}h ago`;
  return new Date(iso).toISOString().slice(0, 10);
}
