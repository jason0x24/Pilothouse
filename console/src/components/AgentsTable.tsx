"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { AgentOut, TemplateOut, relTime, shortId } from "@/lib/api";

/** Client-side filter — instant, no roundtrip. */
export function AgentsTable({
  agents,
  templates,
}: {
  agents: AgentOut[];
  templates: TemplateOut[];
}) {
  const [q, setQ] = useState("");
  const [tpl, setTpl] = useState<string>("");
  const tmap = useMemo(() => Object.fromEntries(templates.map((t) => [t.key, t])), [templates]);

  const filtered = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return agents.filter((a) => {
      if (tpl && a.template !== tpl) return false;
      if (!ql) return true;
      return (
        a.name.toLowerCase().includes(ql) ||
        a.template.toLowerCase().includes(ql) ||
        a.description.toLowerCase().includes(ql)
      );
    });
  }, [agents, q, tpl]);

  return (
    <>
      <div className="card flex flex-wrap gap-3 items-center">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="search by name / template / description"
          className="flex-1 min-w-[220px] rounded bg-ink-900 border border-ink-700 p-2 text-sm"
        />
        <select
          value={tpl}
          onChange={(e) => setTpl(e.target.value)}
          className="rounded bg-ink-900 border border-ink-700 p-2 text-sm"
        >
          <option value="">all templates</option>
          {templates.map((t) => (
            <option key={t.key} value={t.key}>
              {t.name}
            </option>
          ))}
        </select>
        <span className="text-xs text-ink-300">
          {filtered.length} / {agents.length}
        </span>
      </div>
      <section className="card overflow-x-auto">
        {filtered.length === 0 ? (
          <p className="text-ink-300 text-sm">No agents match.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-ink-300">
              <tr>
                <th className="text-left font-normal pb-2">ID</th>
                <th className="text-left font-normal pb-2">Name</th>
                <th className="text-left font-normal pb-2">Template</th>
                <th className="text-left font-normal pb-2">Schedule</th>
                <th className="text-left font-normal pb-2">Dry-run</th>
                <th className="text-left font-normal pb-2">Enabled</th>
                <th className="text-right font-normal pb-2">Updated</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((a) => (
                <tr key={a.id} className="border-t border-ink-700">
                  <td className="py-2 font-mono text-sky-300">
                    <Link href={`/agents/${a.id}`} className="hover:underline">
                      {shortId(a.id)}
                    </Link>
                  </td>
                  <td className="font-medium">{a.name}</td>
                  <td>{tmap[a.template]?.name ?? a.template}</td>
                  <td className="font-mono">{a.schedule_cron ?? "—"}</td>
                  <td>
                    {a.dry_run ? (
                      <span className="pill pill-info">dry-run</span>
                    ) : (
                      <span className="pill pill-warn">live</span>
                    )}
                  </td>
                  <td>
                    {a.enabled ? (
                      <span className="pill pill-ok">on</span>
                    ) : (
                      <span className="pill pill-neutral">off</span>
                    )}
                  </td>
                  <td className="text-right text-ink-300">{relTime(a.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
