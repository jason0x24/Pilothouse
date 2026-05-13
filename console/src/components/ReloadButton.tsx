"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api";

export function ReloadButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [count, setCount] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setErr(null);
    try {
      const { count: n } = await api.pluginReload();
      setCount(n);
      router.refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <button onClick={go} disabled={busy} className="btn-ghost text-xs">
        {busy ? "Reloading…" : "Reload"}
      </button>
      {count !== null && !err && (
        <span className="text-xs text-ink-300">{count} plugin(s) registered</span>
      )}
      {err && <span className="text-xs text-rose-300">{err}</span>}
    </div>
  );
}
