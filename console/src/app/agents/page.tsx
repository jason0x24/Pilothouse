import Link from "next/link";

import { AgentsTable } from "@/components/AgentsTable";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function AgentsPage() {
  const [agents, templates] = await Promise.all([api.agents(), api.templates()]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Agents</h1>
        <Link href="/agents/new" className="btn-primary">
          New agent
        </Link>
      </div>
      {agents.length === 0 ? (
        <section className="card">
          <p className="text-ink-300 text-sm">
            No agents yet — create one with{" "}
            <code className="font-mono">pilothouse agents create</code> or click{" "}
            <strong>New agent</strong>.
          </p>
        </section>
      ) : (
        <AgentsTable agents={agents} templates={templates} />
      )}
    </div>
  );
}
