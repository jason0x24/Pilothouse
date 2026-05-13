"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api";

/** Retry available on terminal runs only — the new run gets a fresh id. */
export function RetryButton({ runId, status }: { runId: string; status: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (status !== "succeeded" && status !== "failed" && status !== "cancelled") return null;

  async function go() {
    setErr(null);
    setBusy(true);
    try {
      const r = await api.retryRun(runId);
      router.push(`/runs/${r.id}`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <button onClick={go} disabled={busy} className="btn-ghost text-xs">
        {busy ? "Retrying…" : "Retry"}
      </button>
      {err && <span className="text-rose-300 text-xs">{err}</span>}
    </div>
  );
}
