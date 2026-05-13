"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

const PUBLIC_API =
  process.env.NEXT_PUBLIC_PILOTHOUSE_API ?? "http://127.0.0.1:8088";

/** Cancellable for `running` and `awaiting_approval` only. The route does
 *  the cancel + refresh so the parent server component re-renders with
 *  the updated status, summary, and event timeline. */
export function CancelButton({ runId, status }: { runId: string; status: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const canCancel = status === "running" || status === "awaiting_approval";
  if (!canCancel) return null;

  async function go() {
    setErr(null);
    setBusy(true);
    try {
      const res = await fetch(`${PUBLIC_API}/runs/${runId}/cancel`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ by: "console" }),
      });
      if (!res.ok) throw new Error(await res.text());
      router.refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <button onClick={go} disabled={busy} className="btn-danger text-xs">
        {busy ? "Cancelling…" : "Cancel run"}
      </button>
      {err && <span className="text-rose-300 text-xs">{err}</span>}
    </div>
  );
}
