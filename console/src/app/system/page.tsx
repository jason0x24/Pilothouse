import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function SystemPage() {
  const [templates, connectors] = await Promise.all([api.templates(), api.connectors()]);
  return (
    <div className="space-y-6">
      <div>
        <div className="text-ink-300 text-xs">SYSTEM</div>
        <h1 className="text-2xl font-semibold">Templates &amp; connectors</h1>
      </div>
      <section className="card">
        <h2 className="text-lg font-semibold mb-3">Templates</h2>
        <table className="w-full text-sm">
          <thead className="text-ink-300">
            <tr>
              <th className="text-left font-normal pb-2">Key</th>
              <th className="text-left font-normal pb-2">Name</th>
              <th className="text-left font-normal pb-2">Default tools</th>
              <th className="text-left font-normal pb-2">Description</th>
            </tr>
          </thead>
          <tbody>
            {templates.map((t) => (
              <tr key={t.key} className="border-t border-ink-700">
                <td className="py-2 font-mono">{t.key}</td>
                <td>{t.name}</td>
                <td className="font-mono text-xs">{t.default_tools.join(", ")}</td>
                <td className="text-ink-300">{t.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="card">
        <h2 className="text-lg font-semibold mb-3">Connectors</h2>
        <div className="grid gap-3 md:grid-cols-2">
          {connectors.map((c) => (
            <div key={c.name} className="rounded border border-ink-700 bg-ink-900 p-3">
              <div className="flex items-center justify-between">
                <div className="font-mono text-sm">{c.name}</div>
                <span className={`pill ${c.live ? "pill-ok" : "pill-info"}`}>
                  {c.live ? "live" : "mock"}
                </span>
              </div>
              <ul className="mt-2 space-y-1 text-xs">
                {c.tools.map((t) => (
                  <li key={t.name} className="flex items-center justify-between gap-2">
                    <code className="font-mono text-ink-100">{t.name}</code>
                    {t.destructive ? (
                      <span className="pill pill-warn">destructive</span>
                    ) : (
                      <span className="pill pill-neutral">read</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
