import Link from "next/link";
import { notFound } from "next/navigation";

import { TriggerPanel } from "@/components/TriggerPanel";
import { StatusPill } from "@/components/StatusPill";
import { api, relTime, shortId } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function AgentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const agent = await api.agent(id).catch(() => null);
  if (!agent) notFound();
  const runs = await api.agentRuns(id).catch(() => []);

  return (
    <div className="space-y-6">
      <div>
        <div className="text-ink-300 text-xs">AGENT</div>
        <h1 className="text-2xl font-semibold">{agent.name}</h1>
      </div>
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-2 space-y-3">
          <dl className="kv">
            <dt>ID</dt>
            <dd>{agent.id}</dd>
            <dt>Template</dt>
            <dd>{agent.template}</dd>
            <dt>Schedule</dt>
            <dd>{agent.schedule_cron ?? "—"}</dd>
            <dt>Dry-run</dt>
            <dd>{agent.dry_run ? "yes" : "no"}</dd>
            <dt>Enabled</dt>
            <dd>{agent.enabled ? "yes" : "no"}</dd>
            <dt>Created</dt>
            <dd>{new Date(agent.created_at).toLocaleString()}</dd>
          </dl>
          {Object.keys(agent.params).length > 0 && (
            <div>
              <div className="text-ink-300 text-xs uppercase tracking-wide mb-1">Params</div>
              <pre className="text-xs font-mono whitespace-pre-wrap rounded bg-ink-900 border border-ink-700 p-3">
                {JSON.stringify(agent.params, null, 2)}
              </pre>
            </div>
          )}
        </div>
        <TriggerPanel agentId={agent.id} />
      </section>

      <section className="card">
        <h2 className="text-lg font-semibold mb-3">Recent runs</h2>
        {runs.length === 0 ? (
          <p className="text-ink-300 text-sm">No runs yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-ink-300">
              <tr>
                <th className="text-left font-normal pb-2">Run</th>
                <th className="text-left font-normal pb-2">Trigger</th>
                <th className="text-left font-normal pb-2">Status</th>
                <th className="text-right font-normal pb-2">Tokens (in/out)</th>
                <th className="text-right font-normal pb-2">When</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-t border-ink-700">
                  <td className="py-2 font-mono">
                    <Link href={`/runs/${r.id}`} className="text-sky-300 hover:underline">
                      {shortId(r.id)}
                    </Link>
                  </td>
                  <td className="text-ink-300">{r.trigger}</td>
                  <td><StatusPill status={r.status} /></td>
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
