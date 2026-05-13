"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

import { ApprovalRow } from "@/components/ApprovalRow";
import { api, ApprovalOut, shortId } from "@/lib/api";

/**
 * Pending-approvals view with bulk-select. The parent page passes the
 * approvals list (already tenant-scoped server-side); we maintain the
 * checkbox set client-side and POST to /approvals/resolve-batch on
 * approve / reject.
 */
export function BulkApprovals({ approvals }: { approvals: ApprovalOut[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const pendingIds = useMemo(
    () => approvals.filter((a) => a.status === "pending").map((a) => a.id),
    [approvals],
  );
  const allSelected = pendingIds.length > 0 && pendingIds.every((id) => selected.has(id));

  function toggle(id: string) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAll() {
    setSelected(allSelected ? new Set() : new Set(pendingIds));
  }

  async function bulk(decision: "approve" | "reject") {
    if (selected.size === 0) {
      setErr("select at least one approval");
      return;
    }
    setErr(null);
    setBusy(decision);
    try {
      await api.resolveApprovalBatch(
        Array.from(selected),
        decision,
        "console",
        decision === "reject" ? reason : "",
      );
      setSelected(new Set());
      setReason("");
      router.refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (approvals.length === 0) return null;

  return (
    <div className="space-y-4">
      {pendingIds.length > 0 && (
        <div className="card flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={allSelected} onChange={selectAll} />
            Select all pending ({pendingIds.length})
          </label>
          <input
            placeholder="optional rejection reason (applied to all)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="flex-1 min-w-[200px] rounded bg-ink-900 border border-ink-700 p-2 text-sm"
          />
          <button
            onClick={() => bulk("approve")}
            disabled={busy !== null || selected.size === 0}
            className="btn-primary text-xs"
          >
            {busy === "approve" ? "Approving…" : `Approve ${selected.size}`}
          </button>
          <button
            onClick={() => bulk("reject")}
            disabled={busy !== null || selected.size === 0}
            className="btn-danger text-xs"
          >
            {busy === "reject" ? "Rejecting…" : `Reject ${selected.size}`}
          </button>
          {err && <span className="text-rose-300 text-xs">{err}</span>}
        </div>
      )}
      <div className="space-y-4">
        {approvals.map((a) => (
          <div key={a.id} className="card">
            <div className="flex items-center justify-between mb-3 text-sm gap-3">
              <div className="flex items-center gap-3">
                {a.status === "pending" && (
                  <input
                    type="checkbox"
                    checked={selected.has(a.id)}
                    onChange={() => toggle(a.id)}
                    aria-label={`Select approval ${shortId(a.id)}`}
                  />
                )}
                <Link
                  href={`/runs/${a.run_id}`}
                  className="font-mono text-sky-300 hover:underline"
                >
                  run {shortId(a.run_id)}
                </Link>
              </div>
              <span className="text-ink-300">
                created {new Date(a.created_at).toLocaleString()}
              </span>
            </div>
            <ApprovalRow approval={a} />
          </div>
        ))}
      </div>
    </div>
  );
}
