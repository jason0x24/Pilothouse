import Link from "next/link";
import { notFound } from "next/navigation";

import { ApprovalRow } from "@/components/ApprovalRow";
import { CancelButton } from "@/components/CancelButton";
import { LiveTimeline } from "@/components/LiveTimeline";
import { RetryButton } from "@/components/RetryButton";
import { StatusPill } from "@/components/StatusPill";
import { api, shortId } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const run = await api.run(id).catch(() => null);
  if (!run) notFound();
  const [approvals, agent] = await Promise.all([
    api.runApprovals(id).catch(() => []),
    api.agent(run.agent_id).catch(() => null),
  ]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link href="/agents" className="text-ink-300 text-sm hover:text-white">
          Agents
        </Link>
        <span className="text-ink-500">/</span>
        {agent ? (
          <Link href={`/agents/${agent.id}`} className="text-ink-200 hover:text-white">
            {agent.name}
          </Link>
        ) : (
          <span>(unknown agent)</span>
        )}
        <span className="text-ink-500">/</span>
        <span className="font-mono">{shortId(run.id)}</span>
        <StatusPill status={run.status} />
        <div className="ml-auto flex items-center gap-2">
          <CancelButton runId={run.id} status={run.status} />
          <RetryButton runId={run.id} status={run.status} />
        </div>
      </div>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-2 space-y-3">
          <dl className="kv">
            <dt>Run</dt>
            <dd>{run.id}</dd>
            <dt>Trigger</dt>
            <dd>{run.trigger}</dd>
            <dt>Started</dt>
            <dd>{new Date(run.started_at).toLocaleString()}</dd>
            <dt>Finished</dt>
            <dd>{run.finished_at ? new Date(run.finished_at).toLocaleString() : "—"}</dd>
            <dt>Tokens (in / out)</dt>
            <dd>
              {run.tokens_input} / {run.tokens_output}
            </dd>
            <dt>Estimated cost</dt>
            <dd>${(run.cost_usd_cents / 10000).toFixed(4)}</dd>
          </dl>
        </div>
        <div className="card">
          <h3 className="text-sm uppercase tracking-wide text-ink-300 mb-2">Summary</h3>
          <pre className="whitespace-pre-wrap text-sm leading-relaxed">{run.summary || "(empty)"}</pre>
        </div>
      </section>

      {approvals.length > 0 && (
        <section className="card">
          <h2 className="text-lg font-semibold mb-3">Approvals</h2>
          <div className="space-y-3">
            {approvals.map((a) => (
              <ApprovalRow key={a.id} approval={a} />
            ))}
          </div>
        </section>
      )}

      <section className="card">
        <LiveTimeline runId={run.id} initialStatus={run.status} />
      </section>
    </div>
  );
}
