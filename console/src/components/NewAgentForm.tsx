"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, TemplateOut } from "@/lib/api";

export function NewAgentForm({ templates }: { templates: TemplateOut[] }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [template, setTemplate] = useState(templates[0]?.key ?? "");
  const [description, setDescription] = useState("");
  const [paramsText, setParamsText] = useState("{}");
  const [cron, setCron] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      let params: Record<string, unknown> = {};
      if (paramsText.trim()) {
        try {
          params = JSON.parse(paramsText);
        } catch {
          throw new Error("Params must be valid JSON");
        }
      }
      const agent = await api.createAgent({
        name,
        template,
        description,
        params,
        schedule_cron: cron.trim() || null,
        dry_run: dryRun,
      });
      router.push(`/agents/${agent.id}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const activeTemplate = templates.find((t) => t.key === template);

  return (
    <form onSubmit={submit} className="card space-y-4">
      <div>
        <label className="block text-xs uppercase text-ink-300 tracking-wide">Name</label>
        <input
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="checkout-triage"
          className="mt-1 w-full rounded bg-ink-900 border border-ink-700 p-2 font-mono text-sm"
        />
      </div>
      <div>
        <label className="block text-xs uppercase text-ink-300 tracking-wide">Template</label>
        <select
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          className="mt-1 w-full rounded bg-ink-900 border border-ink-700 p-2 text-sm"
        >
          {templates.map((t) => (
            <option key={t.key} value={t.key}>
              {t.name} ({t.key})
            </option>
          ))}
        </select>
        {activeTemplate && (
          <p className="text-xs text-ink-300 mt-1">
            {activeTemplate.description} · default tools:{" "}
            <code className="font-mono">{activeTemplate.default_tools.join(", ")}</code>
          </p>
        )}
      </div>
      <div>
        <label className="block text-xs uppercase text-ink-300 tracking-wide">
          Params (JSON)
        </label>
        <textarea
          rows={6}
          value={paramsText}
          onChange={(e) => setParamsText(e.target.value)}
          className="mt-1 w-full rounded bg-ink-900 border border-ink-700 p-2 font-mono text-xs"
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs uppercase text-ink-300 tracking-wide">
            Cron schedule (optional)
          </label>
          <input
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            placeholder="*/15 * * * *"
            className="mt-1 w-full rounded bg-ink-900 border border-ink-700 p-2 font-mono text-sm"
          />
        </div>
        <div className="flex items-end gap-2">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(e) => setDryRun(e.target.checked)}
            />
            Start in dry-run
          </label>
        </div>
      </div>
      <div>
        <label className="block text-xs uppercase text-ink-300 tracking-wide">Description</label>
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className="mt-1 w-full rounded bg-ink-900 border border-ink-700 p-2 text-sm"
        />
      </div>
      {err && <p className="text-rose-300 text-sm">{err}</p>}
      <button className="btn-primary" disabled={busy}>
        {busy ? "Creating…" : "Create agent"}
      </button>
    </form>
  );
}
