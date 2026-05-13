import { classifyStatus } from "@/lib/api";

export function StatusPill({ status }: { status: string }) {
  const klass = classifyStatus(status);
  return <span className={`pill pill-${klass}`}>{status}</span>;
}
