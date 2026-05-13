import Link from "next/link";

import { StatusPill } from "@/components/StatusPill";
import { api, relTime, shortId } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  // The dashboard is a thin overview: counts + the latest few runs across
  // all agents. We compute it client-side from /agents + /agents/{id}/runs
  // for now — a future server-side `/runs?limit=N` would tidy this up.
  const agents = await api.agents().catch(() => []);
  const recentRuns = (
    await Promise.all(
      agents.map(async (a) => {
        try {
          return (await api.agentRuns(a.id)).slice(0, 5).map((r) => ({ ...r, agent_name: a.name }));
        } catch {
          return [];
        }
      })
    )
  )
    .flat()
    .sort((a, b) => +new Date(b.started_at) - +new Date(a.started_at))
    .slice(0, 12);

  const approvalsPending = await api.approvals("pending").catch(() => []);

  return (
    <div className="space-y-6">
      <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Stat label="Agents" value={agents.length} sub="configured" href="/agents" />
        <Stat
          label="Pending approvals"
          value={approvalsPending.length}
          sub={approvalsPending.length ? "awaiting decision" : "none"}
          href="/approvals"
          highlight={approvalsPending.length > 0}
        />
        <Stat
          label="Runs in last 5"
          value={recentRuns.length}
          sub="across all agents"
        />
      </section>

      <section className="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Recent runs</h2>
          <Link href="/agents" className="text-sm text-ink-300 hover:text-white">
            View all →
          </Link>
        </div>
        {recentRuns.length === 0 ? (
          <p className="text-ink-300 text-sm">
            No runs yet. Create an agent and trigger it from the CLI:{" "}
            <code className="font-mono">pilothouse demo</code>.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-ink-300">
              <tr>
                <th className="text-left font-normal pb-2">Run</th>
                <th className="text-left font-normal pb-2">Agent</th>
                <th className="text-left font-normal pb-2">Status</th>
                <th className="text-left font-normal pb-2">Trigger</th>
                <th className="text-right font-normal pb-2">Tokens</th>
                <th className="text-right font-normal pb-2">When</th>
              </tr>
            </thead>
            <tbody>
              {recentRuns.map((r) => (
                <tr key={r.id} className="border-t border-ink-700">
                  <td className="py-2">
                    <Link href={`/runs/${r.id}`} className="font-mono text-sky-300 hover:underline">
                      {shortId(r.id)}
                    </Link>
                  </td>
                  <td>{r.agent_name}</td>
                  <td>
                    <StatusPill status={r.status} />
                  </td>
                  <td className="text-ink-300">{r.trigger}</td>
                  <td className="text-right font-mono">
                    {r.tokens_input}/{r.tokens_output}
                  </td>
                  <td className="text-right text-ink-300">{relTime(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  href,
  highlight,
}: {
  label: string;
  value: number | string;
  sub: string;
  href?: string;
  highlight?: boolean;
}) {
  const card = (
    <div className={`card ${highlight ? "border-amber-700" : ""}`}>
      <div className="text-ink-300 text-xs uppercase tracking-wide">{label}</div>
      <div className={`mt-1 text-3xl font-semibold ${highlight ? "text-amber-200" : ""}`}>
        {value}
      </div>
      <div className="text-ink-300 text-xs">{sub}</div>
    </div>
  );
  return href ? <Link href={href}>{card}</Link> : card;
}
