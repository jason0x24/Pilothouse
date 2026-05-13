import Link from "next/link";

import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function DashboardPage({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const params = await searchParams;
  const days = Math.max(1, Math.min(90, Number(params.days ?? 7) || 7));
  const stats = await api.stats(days);

  // For the bar chart we normalise against the day with the most cost.
  const maxCost = Math.max(0.0001, ...stats.by_day.map((d) => d.cost_usd));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Cost &amp; usage</h1>
        <div className="flex gap-2 text-sm">
          {[1, 7, 30].map((n) => (
            <Link
              key={n}
              href={`/dashboard?days=${n}`}
              className={`pill ${n === days ? "pill-info" : "pill-neutral"} hover:opacity-80`}
            >
              {n === 1 ? "today" : `${n}d`}
            </Link>
          ))}
        </div>
      </div>

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-4">
        <Stat label="Runs" value={stats.totals.runs} />
        <Stat label="Input tokens" value={stats.totals.tokens_in.toLocaleString()} />
        <Stat label="Output tokens" value={stats.totals.tokens_out.toLocaleString()} />
        <Stat label="Estimated cost" value={`$${stats.totals.cost_usd.toFixed(4)}`} highlight />
      </section>

      <section className="card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Cost per day</h2>
          <span className="text-xs text-ink-300">
            scaled to max ${maxCost.toFixed(4)}
          </span>
        </div>
        {stats.by_day.length === 0 ? (
          <p className="text-sm text-ink-300">No runs in the window.</p>
        ) : (
          <ol className="space-y-2">
            {stats.by_day.map((d) => (
              <li key={d.date} className="flex items-center gap-3 text-sm">
                <span className="w-24 font-mono text-ink-300">{d.date}</span>
                <div className="flex-1 h-5 rounded bg-ink-900 border border-ink-700 overflow-hidden">
                  <div
                    className="h-full bg-emerald-700"
                    style={{ width: `${(d.cost_usd / maxCost) * 100}%` }}
                    title={`${d.runs} runs · $${d.cost_usd.toFixed(4)}`}
                  />
                </div>
                <span className="w-20 text-right font-mono">{d.runs}</span>
                <span className="w-24 text-right font-mono">
                  ${d.cost_usd.toFixed(4)}
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="card">
          <h2 className="text-lg font-semibold mb-3">Top agents by cost</h2>
          {stats.by_agent.length === 0 ? (
            <p className="text-sm text-ink-300">No runs to show.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-ink-300">
                <tr>
                  <th className="text-left font-normal pb-2">Agent</th>
                  <th className="text-right font-normal pb-2">Runs</th>
                  <th className="text-right font-normal pb-2">Tokens (in/out)</th>
                  <th className="text-right font-normal pb-2">Cost</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_agent.slice(0, 10).map((a) => (
                  <tr key={a.agent} className="border-t border-ink-700">
                    <td className="py-2 font-medium">{a.agent}</td>
                    <td className="text-right font-mono">{a.runs}</td>
                    <td className="text-right font-mono">
                      {a.tokens_in}/{a.tokens_out}
                    </td>
                    <td className="text-right font-mono">${a.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="card">
          <h2 className="text-lg font-semibold mb-3">Run outcomes</h2>
          {Object.keys(stats.by_status).length === 0 ? (
            <p className="text-sm text-ink-300">No outcomes recorded.</p>
          ) : (
            <ul className="space-y-2 text-sm">
              {Object.entries(stats.by_status)
                .sort(([, a], [, b]) => b - a)
                .map(([status, count]) => (
                  <li key={status} className="flex items-center justify-between">
                    <span className={`pill pill-${pillClass(status)}`}>{status}</span>
                    <span className="font-mono">{count}</span>
                  </li>
                ))}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  highlight,
}: {
  label: string;
  value: number | string;
  highlight?: boolean;
}) {
  return (
    <div className={`card ${highlight ? "border-emerald-700" : ""}`}>
      <div className="text-ink-300 text-xs uppercase tracking-wide">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${highlight ? "text-emerald-200" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function pillClass(status: string): string {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "err";
  if (status === "awaiting_approval" || status === "running") return "warn";
  if (status === "cancelled") return "neutral";
  return "info";
}
