import { PluginCard } from "@/components/PluginCard";
import { ReloadButton } from "@/components/ReloadButton";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

const KIND_ORDER = ["template", "connector", "notifier", "trigger", "hook"];

export default async function PluginsPage({
  searchParams,
}: {
  searchParams: Promise<{ kind?: string; status?: string }>;
}) {
  const params = await searchParams;
  const all = await api.plugins().catch(() => []);

  let filtered = all;
  if (params.kind) {
    filtered = filtered.filter((p) => p.kinds.includes(params.kind!));
  }
  if (params.status === "misconfigured") {
    filtered = filtered.filter((p) => p.enabled && p.misconfig_reason);
  } else if (params.status === "off") {
    filtered = filtered.filter((p) => !p.enabled);
  } else if (params.status === "on") {
    filtered = filtered.filter((p) => p.enabled && !p.misconfig_reason);
  }

  const misconfigured = all.filter((p) => p.enabled && p.misconfig_reason);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Plugins</h1>
        <ReloadButton />
      </div>

      {misconfigured.length > 0 && (
        <div className="card border-rose-700">
          <div className="font-medium text-rose-200 mb-2">
            ⚠ {misconfigured.length} plugin(s) enabled but misconfigured
          </div>
          <ul className="text-sm space-y-1">
            {misconfigured.map((p) => (
              <li key={p.name}>
                <code className="font-mono">{p.name}</code> — {p.misconfig_reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap gap-2 text-xs">
        <FilterPill href="/plugins" label="all" active={!params.kind && !params.status} />
        {(["on", "off", "misconfigured"] as const).map((s) => (
          <FilterPill
            key={s}
            href={`/plugins?status=${s}`}
            label={s}
            active={params.status === s && !params.kind}
          />
        ))}
        <span className="mx-2 text-ink-500">·</span>
        {KIND_ORDER.map((k) => (
          <FilterPill
            key={k}
            href={`/plugins?kind=${k}`}
            label={k}
            active={params.kind === k}
          />
        ))}
      </div>

      <div className="grid grid-cols-1 gap-3">
        {filtered.length === 0 ? (
          <p className="text-sm text-ink-300">No plugins match this filter.</p>
        ) : (
          filtered.map((p) => <PluginCard key={p.name} plugin={p} />)
        )}
      </div>

      <p className="text-xs text-ink-300">
        Drop plugins into <code className="font-mono">PILOTHOUSE_PLUGIN_DIR</code>{" "}
        (or install via pip with a <code className="font-mono">pilothouse.plugins</code>{" "}
        entry point) and click <strong>Reload</strong> to discover them without a restart.
      </p>
    </div>
  );
}

function FilterPill({
  href,
  label,
  active,
}: {
  href: string;
  label: string;
  active: boolean;
}) {
  return (
    <a
      href={href}
      className={`pill ${active ? "pill-info" : "pill-neutral"} hover:opacity-80`}
    >
      {label}
    </a>
  );
}
