import Link from "next/link";

import { api, shortId } from "@/lib/api";

export const dynamic = "force-dynamic";

function relTo(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const d = t - Date.now();
  if (Number.isNaN(t)) return "—";
  if (d < 0) return "imminent";
  const m = Math.floor(d / 60_000);
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `in ${h}h ${m % 60}m`;
  return `in ${Math.floor(h / 24)}d`;
}

export default async function SchedulePage() {
  const items = await api.schedule().catch(() => []);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Scheduled agents</h1>
        <span className="text-sm text-ink-300">{items.length} on a schedule</span>
      </div>
      <section className="card overflow-x-auto">
        {items.length === 0 ? (
          <p className="text-ink-300 text-sm">
            No scheduled agents. Add a <code className="font-mono">schedule_cron</code>{" "}
            field on an agent and it'll appear here.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-ink-300">
              <tr>
                <th className="text-left font-normal pb-2">ID</th>
                <th className="text-left font-normal pb-2">Name</th>
                <th className="text-left font-normal pb-2">Template</th>
                <th className="text-left font-normal pb-2">Cron</th>
                <th className="text-left font-normal pb-2">Next fire</th>
                <th className="text-right font-normal pb-2">When</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id} className="border-t border-ink-700">
                  <td className="py-2 font-mono">
                    <Link
                      href={`/agents/${s.id}`}
                      className="text-sky-300 hover:underline"
                    >
                      {shortId(s.id)}
                    </Link>
                  </td>
                  <td className="font-medium">{s.name}</td>
                  <td className="text-ink-300">{s.template}</td>
                  <td className="font-mono">{s.cron}</td>
                  <td className="font-mono text-ink-200">
                    {s.next_fire ? new Date(s.next_fire).toLocaleString() : "(invalid cron)"}
                  </td>
                  <td className="text-right text-ink-300">{relTo(s.next_fire)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
