"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, ApprovalOut } from "@/lib/api";
import { StatusPill } from "@/components/StatusPill";

export function ApprovalRow({ approval }: { approval: ApprovalOut }) {
  const router = useRouter();
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const isPending = approval.status === "pending";

  async function resolve(decision: "approve" | "reject") {
    setErr(null);
    setBusy(decision);
    try {
      await api.resolveApproval(approval.id, decision, "console", reason);
      router.refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="rounded border border-ink-700 bg-ink-900 p-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-xs text-ink-300">tool</div>
          <div className="font-mono">{approval.tool_name}</div>
        </div>
        <StatusPill status={approval.status} />
      </div>
      {approval.rationale && (
        <div className="mt-2">
          <div className="text-xs uppercase text-ink-300">Rationale</div>
          <p className="text-sm whitespace-pre-wrap">{approval.rationale}</p>
        </div>
      )}
      <div className="mt-2">
        <div className="text-xs uppercase text-ink-300">Tool input</div>
        <pre className="text-xs font-mono whitespace-pre-wrap rounded bg-ink-800 border border-ink-700 p-2">
          {JSON.stringify(approval.tool_input, null, 2)}
        </pre>
      </div>
      {!isPending && (
        <div className="text-xs text-ink-300 mt-2">
          {approval.resolved_at && (
            <>resolved by <strong>{approval.resolved_by}</strong> at {new Date(approval.resolved_at).toLocaleString()}</>
          )}
          {approval.rejection_reason && <> — reason: {approval.rejection_reason}</>}
        </div>
      )}
      {isPending && (
        <div className="mt-3 space-y-2">
          <input
            placeholder="optional rejection reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="w-full rounded bg-ink-800 border border-ink-700 p-2 text-sm"
          />
          {err && <p className="text-rose-300 text-xs">{err}</p>}
          <div className="flex gap-2">
            <button
              onClick={() => resolve("approve")}
              disabled={busy !== null}
              className="btn-primary"
            >
              {busy === "approve" ? "Approving…" : "Approve & resume"}
            </button>
            <button
              onClick={() => resolve("reject")}
              disabled={busy !== null}
              className="btn-danger"
            >
              {busy === "reject" ? "Rejecting…" : "Reject"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
