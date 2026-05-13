"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api";

type Plugin = Awaited<ReturnType<typeof api.plugins>>[number];

const KIND_PILL: Record<string, string> = {
  template: "pill-info",
  connector: "pill-info",
  notifier: "pill-warn",
  trigger: "pill-warn",
  hook: "pill-neutral",
};

/** One plugin card with inline enable/disable + config editor.
 *
 * The config editor is a thin form per declared schema field. Secrets
 * are masked in the API response — we render a "set new value" input
 * rather than the current value to avoid showing operators a value
 * they don't have permission to read. */
export function PluginCard({ plugin }: { plugin: Plugin }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    setBusy(true);
    setError(null);
    try {
      if (plugin.enabled) await api.pluginDisable(plugin.name);
      else await api.pluginEnable(plugin.name);
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const statusClass = !plugin.enabled
    ? "pill-neutral"
    : plugin.misconfig_reason
      ? "pill-err"
      : "pill-ok";
  const statusText = !plugin.enabled
    ? "off"
    : plugin.misconfig_reason
      ? "misconfigured"
      : "on";

  return (
    <div className="card space-y-3">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex-1 min-w-[240px]">
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm font-medium">{plugin.name}</span>
            <span className={`pill ${statusClass}`}>{statusText}</span>
            {plugin.kinds.map((k) => (
              <span key={k} className={`pill ${KIND_PILL[k] ?? "pill-neutral"}`}>
                {k}
              </span>
            ))}
          </div>
          <p className="text-xs text-ink-300 mt-1">{plugin.description}</p>
          <p className="text-xs text-ink-300 mt-0.5">
            v{plugin.version || "?"} · {plugin.source}
          </p>
          {plugin.misconfig_reason && (
            <p className="text-xs text-rose-300 mt-1">⚠ {plugin.misconfig_reason}</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {plugin.config_schema.length > 0 && (
            <button
              onClick={() => setShowConfig((v) => !v)}
              className="btn-ghost text-xs"
            >
              {showConfig ? "Hide config" : "Config"}
            </button>
          )}
          <button
            onClick={toggle}
            disabled={busy}
            className={plugin.enabled ? "btn-danger text-xs" : "btn-primary text-xs"}
          >
            {busy ? "…" : plugin.enabled ? "Disable" : "Enable"}
          </button>
        </div>
      </div>
      {error && <p className="text-rose-300 text-xs">{error}</p>}
      {showConfig && (
        <PluginConfigEditor plugin={plugin} />
      )}
    </div>
  );
}

function PluginConfigEditor({ plugin }: { plugin: Plugin }) {
  const router = useRouter();
  const [values, setValues] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [errs, setErrs] = useState<Record<string, string>>({});

  async function save(key: string) {
    setSavingKey(key);
    setErrs((e) => ({ ...e, [key]: "" }));
    try {
      await api.pluginSetConfig(plugin.name, key, values[key] ?? "");
      setValues((v) => ({ ...v, [key]: "" }));
      router.refresh();
    } catch (e) {
      setErrs((eo) => ({ ...eo, [key]: (e as Error).message }));
    } finally {
      setSavingKey(null);
    }
  }

  async function unset(key: string) {
    setSavingKey(key);
    setErrs((e) => ({ ...e, [key]: "" }));
    try {
      await api.pluginUnsetConfig(plugin.name, key);
      router.refresh();
    } catch (e) {
      setErrs((eo) => ({ ...eo, [key]: (e as Error).message }));
    } finally {
      setSavingKey(null);
    }
  }

  return (
    <div className="rounded border border-ink-700 bg-ink-900 p-3 space-y-3">
      <div className="text-xs uppercase tracking-wide text-ink-300">Config</div>
      {plugin.config_schema.map((f) => (
        <div key={f.name} className="space-y-1">
          <div className="flex items-center gap-2 text-sm">
            <span className="font-mono">{f.name}</span>
            {f.required && <span className="pill pill-warn">required</span>}
            {f.secret && <span className="pill pill-neutral">secret</span>}
            {f.env_fallback && (
              <span className="text-xs text-ink-300">
                env fallback: <code className="font-mono">{f.env_fallback}</code>
              </span>
            )}
          </div>
          {f.description && (
            <p className="text-xs text-ink-300">{f.description}</p>
          )}
          <div className="flex gap-2">
            <input
              type={f.secret ? "password" : "text"}
              placeholder={f.secret ? "(set new value)" : f.default || "(no value)"}
              value={values[f.name] ?? ""}
              onChange={(e) =>
                setValues((v) => ({ ...v, [f.name]: e.target.value }))
              }
              className="flex-1 rounded bg-ink-800 border border-ink-700 p-2 font-mono text-xs"
            />
            <button
              onClick={() => save(f.name)}
              disabled={savingKey === f.name || !(values[f.name] ?? "")}
              className="btn-primary text-xs"
            >
              {savingKey === f.name ? "…" : "Set"}
            </button>
            <button
              onClick={() => unset(f.name)}
              disabled={savingKey === f.name}
              className="btn-ghost text-xs"
            >
              Unset
            </button>
          </div>
          {errs[f.name] && (
            <p className="text-xs text-rose-300">{errs[f.name]}</p>
          )}
        </div>
      ))}
    </div>
  );
}
