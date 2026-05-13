import { NewAgentForm } from "@/components/NewAgentForm";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function NewAgentPage() {
  const templates = await api.templates();
  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <div className="text-ink-300 text-xs">NEW AGENT</div>
        <h1 className="text-2xl font-semibold">Create an agent</h1>
      </div>
      <NewAgentForm templates={templates} />
    </div>
  );
}
