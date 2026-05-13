import Link from "next/link";

import { StatusPill } from "@/components/StatusPill";
import { api, relTime, shortId } from "@/lib/api";

export const dynamic = "force-dynamic";

const STATUSES = [
  "all",
  "succeeded",
  "failed",
  "cancelled",
  "awaiting_approval",
  "running",
  "pending",
];

export default async function RunsPage({
  searchParams,
}: {
  searchParams: Promise<{
    status?: string;
    agent?: string;
    trigger?: string;
    q?: string;
    offset?: string;
  }>;
}) {
  const params = await searchParams;
  const status = params.status && params.status !== "all" ? params.status : undefined;
  const offset = Math.max(0, Number(params.offset ?? 0) || 0);
  const limit = 50;

  const [runs, agents] = await Promise.all([
    api.searchRuns({
      status,
      agent: params.agent,
      trigger: params.trigger,
      q: params.q,
      limit,
      offset,
    }),
    api.agents().catch(() => []),
  ]);
  const agentNameById = Object.fromEntries(agents.map((a) => [a.id, a.name]));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Runs</h1>
        <span className="text-sm text-ink-300">{runs.length} shown</span>
      </div>

      <form className="card flex flex-wrap items-center gap-3" method="get" action="/runs">
        <select
          name="status"
          defaultValue={params.status ?? "all"}
          className="rounded bg-ink-900 border border-ink-700 p-2 text-sm"
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          name="agent"
          defaultValue={params.agent ?? ""}
          className="rounded bg-ink-900 border border-ink-700 p-2 text-sm"
        >
          <option value="">all agents</option>
          {agents.map((a) => (
            <option key={a.id} value={a.name}>
              {a.name}
            </option>
          ))}
        </select>
        <input
          name="trigger"
          defaultValue={params.trigger ?? ""}
          placeholder="trigger contains…"
          className="rounded bg-ink-900 border border-ink-700 p-2 text-sm w-48"
        />
        <input
          name="q"
          defaultValue={params.q ?? ""}
          placeholder="summary contains…"
          className="rounded bg-ink-900 border border-ink-700 p-2 text-sm flex-1 min-w-[160px]"
        />
        <button className="btn-primary text-xs">Search</button>
      </form>

      <section className="card overflow-x-auto">
        {runs.length === 0 ? (
          <p className="text-ink-300 text-sm">No runs match.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-ink-300">
              <tr>
                <th className="text-left font-normal pb-2">Run</th>
                <th className="text-left font-normal pb-2">Agent</th>
                <th className="text-left font-normal pb-2">Status</th>
                <th className="text-left font-normal pb-2">Trigger</th>
                <th className="text-right font-normal pb-2">Tokens</th>
                <th className="text-right font-normal pb-2">Cost</th>
                <th className="text-right font-normal pb-2">When</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-t border-ink-700">
                  <td className="py-2">
                    <Link href={`/runs/${r.id}`} className="font-mono text-sky-300 hover:underline">
                      {shortId(r.id)}
                    </Link>
                  </td>
                  <td>{agentNameById[r.agent_id] ?? shortId(r.agent_id)}</td>
                  <td>
                    <StatusPill status={r.status} />
                  </td>
                  <td className="text-ink-300">{r.trigger}</td>
                  <td className="text-right font-mono">
                    {r.tokens_input}/{r.tokens_output}
                  </td>
                  <td className="text-right font-mono">
                    ${(r.cost_usd_cents / 10000).toFixed(4)}
                  </td>
                  <td className="text-right text-ink-300">{relTime(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {(offset > 0 || runs.length === limit) && (
        <div className="flex justify-between text-sm">
          {offset > 0 ? (
            <Link
              href={buildHref(params, Math.max(0, offset - limit))}
              className="btn-ghost text-xs"
            >
              ← previous
            </Link>
          ) : (
            <span />
          )}
          {runs.length === limit ? (
            <Link
              href={buildHref(params, offset + limit)}
              className="btn-ghost text-xs"
            >
              next →
            </Link>
          ) : (
            <span />
          )}
        </div>
      )}
    </div>
  );
}

function buildHref(
  params: Record<string, string | undefined>,
  offset: number,
): string {
  const sp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v && k !== "offset") sp.set(k, v);
  });
  if (offset > 0) sp.set("offset", String(offset));
  return `/runs${sp.toString() ? `?${sp.toString()}` : ""}`;
}
