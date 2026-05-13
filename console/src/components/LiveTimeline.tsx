"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { EventOut } from "@/lib/api";

const PUBLIC_API =
  process.env.NEXT_PUBLIC_PILOTHOUSE_API ?? "http://127.0.0.1:8088";

/**
 * Connects to /runs/{id}/events/stream and renders events as they arrive.
 *
 * The SSE endpoint replays existing events first then attaches to the
 * live bus, so we can render the full timeline from this one component
 * without an initial fetch.
 *
 * When we receive a terminal event (`run_finished` / `run_cancelled`)
 * we refresh the surrounding server component so the page header (run
 * status pill, tokens, summary) updates.
 */
export function LiveTimeline({ runId, initialStatus }: { runId: string; initialStatus: string }) {
  const router = useRouter();
  const [events, setEvents] = useState<EventOut[]>([]);
  const [connected, setConnected] = useState(false);
  const [terminal, setTerminal] = useState(
    initialStatus === "succeeded" ||
      initialStatus === "failed" ||
      initialStatus === "cancelled"
  );
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (terminal) return;
    const url = `${PUBLIC_API}/runs/${runId}/events/stream`;
    const es = new EventSource(url);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("end", () => {
      es.close();
      setConnected(false);
      setTerminal(true);
      router.refresh();
    });
    // Catch every named event by hooking onmessage and parsing.
    const handler = (e: MessageEvent) => {
      try {
        const payload = JSON.parse(e.data) as EventOut & { run_id?: string };
        if (!payload.id) {
          payload.id = `${payload.kind}-${payload.created_at}`;
        }
        if (seen.current.has(payload.id)) return;
        seen.current.add(payload.id);
        setEvents((prev) => [...prev, payload as EventOut]);
      } catch {
        /* ignore malformed */
      }
    };
    // SSE named events: subscribe with addEventListener for each kind we care
    // about. We subscribe broadly via a catch-all "message" + every specific
    // kind we know, so any future event kinds also flow through.
    const kinds = [
      "run_started",
      "model_turn",
      "tool_call",
      "tool_result",
      "decision",
      "approval_requested",
      "approval_resolved",
      "approval_expired",
      "run_cancelled",
      "run_finished",
      "error",
    ];
    for (const k of kinds) es.addEventListener(k, handler);
    es.addEventListener("message", handler);
    return () => es.close();
  }, [runId, terminal, router]);

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">Event timeline</h2>
        <div className="flex items-center gap-2 text-xs">
          {terminal ? (
            <span className="pill pill-neutral">finalised</span>
          ) : connected ? (
            <span className="pill pill-ok">live</span>
          ) : (
            <span className="pill pill-warn">reconnecting</span>
          )}
          <span className="text-ink-300">{events.length} events</span>
        </div>
      </div>
      {events.length === 0 ? (
        <p className="text-ink-300 text-sm">Waiting for first event…</p>
      ) : (
        <ol className="space-y-3">
          {events.map((ev) => (
            <li key={ev.id} className="border-l-2 border-ink-600 pl-3">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-ink-300">
                  {new Date(ev.created_at).toLocaleTimeString()}
                </span>
                <KindPill kind={ev.kind} />
              </div>
              <pre className="mt-1 text-xs whitespace-pre-wrap break-words font-mono bg-ink-900 border border-ink-700 p-2 rounded">
                {JSON.stringify(ev.data, null, 2)}
              </pre>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function KindPill({ kind }: { kind: string }) {
  let klass = "pill-neutral";
  if (kind === "tool_call" || kind === "tool_result") klass = "pill-info";
  if (kind === "approval_requested") klass = "pill-warn";
  if (kind === "approval_resolved") klass = "pill-ok";
  if (kind === "approval_expired") klass = "pill-warn";
  if (kind === "run_cancelled") klass = "pill-err";
  if (kind === "error") klass = "pill-err";
  if (kind === "run_started" || kind === "run_finished") klass = "pill-ok";
  return <span className={`pill ${klass}`}>{kind}</span>;
}
