"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";

const DEFAULT_PAYLOAD = `{
  "alert_id": "manual-trigger"
}`;

export function TriggerPanel({ agentId }: { agentId: string }) {
  const router = useRouter();
  const [payload, setPayload] = useState(DEFAULT_PAYLOAD);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function trigger(dryRun: boolean) {
    setErr(null);
    setBusy(true);
    try {
      let parsed: Record<string, unknown> = {};
      try {
        parsed = JSON.parse(payload);
      } catch (e) {
        throw new Error("Payload is not valid JSON");
      }
      const run = await api.triggerAgent(agentId, parsed, dryRun);
      router.push(`/runs/${run.id}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card space-y-3">
      <div>
        <div className="text-ink-300 text-xs uppercase tracking-wide">Manual trigger</div>
        <div className="text-xs text-ink-300">JSON payload (the trigger_payload).</div>
      </div>
      <textarea
        rows={6}
        value={payload}
        onChange={(e) => setPayload(e.target.value)}
        className="w-full rounded bg-ink-900 border border-ink-700 p-2 font-mono text-xs"
      />
      {err && <p className="text-rose-300 text-xs">{err}</p>}
      <div className="flex gap-2">
        <button onClick={() => trigger(true)} disabled={busy} className="btn-ghost flex-1">
          {busy ? "…" : "Dry-run"}
        </button>
        <button onClick={() => trigger(false)} disabled={busy} className="btn-primary flex-1">
          {busy ? "…" : "Run live"}
        </button>
      </div>
    </div>
  );
}
