import Link from "next/link";

import { BulkApprovals } from "@/components/BulkApprovals";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ApprovalsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const params = await searchParams;
  const status = params.status ?? "pending";
  const approvals = await api
    .approvals(status === "all" ? undefined : status)
    .catch(() => []);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Approvals</h1>
        <div className="flex gap-2 text-sm">
          {["pending", "approved", "rejected", "all"].map((s) => (
            <Link
              key={s}
              href={`/approvals?status=${s}`}
              className={`pill ${
                s === status ? "pill-info" : "pill-neutral"
              } hover:opacity-80`}
            >
              {s}
            </Link>
          ))}
        </div>
      </div>
      {approvals.length === 0 ? (
        <p className="text-ink-300 text-sm">No approvals matching this filter.</p>
      ) : (
        <BulkApprovals approvals={approvals} />
      )}
    </div>
  );
}
