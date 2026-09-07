"use client";
import { useEffect, useRef, useState } from "react";

export type MetricEvent = { event: string; timestamp: number; [k: string]: any };

type TurnEntry = {
  turn_id: number;
  text?: string;
  tools: { source?: string; status: "pending" | "discarded"; chunks?: number }[];
  ts: number;
  /** How the agent classified the utterance that opened this turn. */
  intent?: string;
  /** Whether in-flight tool work survived that utterance. */
  workKept?: boolean;
  /** Time from end-of-speech to the first Rime audio frame, in ms. */
  ttfaMs?: number;
  /** Fraction of an interrupted reply the user actually heard. */
  heardFraction?: number;
};

function percentile(values: number[], pct: number): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(Math.floor(sorted.length * pct), sorted.length - 1);
  return sorted[idx];
}

export function useMetricsSocket(url = "ws://localhost:8765") {
  const [events, setEvents] = useState<MetricEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [interruptFlash, setInterruptFlash] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const ws = new WebSocket(url);
    wsRef.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (msg) => {
      const evt: MetricEvent = JSON.parse(msg.data);
      setEvents((prev) => [...prev, evt].slice(-500)); // cap for memory
      if (evt.event === "interrupt_detected") {
        setInterruptFlash(true);
        setTimeout(() => setInterruptFlash(false), 600);
      }
    };
    return () => ws.close();
  }, [url]);

  // derive current status
  const last = events[events.length - 1];
  const currentTurn: number | undefined =
    last?.current_turn_id ?? last?.new_turn_id ?? last?.turn_id;

  let status: "idle" | "speaking" | "tool_running" | "interrupted" = "idle";
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i].event;
    if (e === "interrupt_detected") { status = "interrupted"; break; }
    if (e === "assistant_item_added") { status = "speaking"; break; }
    if (e === "stale_result_discarded") { continue; }
  }

  // derive timeline (group by turn)
  const timeline: TurnEntry[] = [];
  const byTurn = new Map<number, TurnEntry>();
  for (const e of events) {
    const tid = e.turn_id ?? e.current_turn_id ?? e.new_turn_id;
    if (tid === undefined) continue;
    if (!byTurn.has(tid)) {
      const entry: TurnEntry = { turn_id: tid, tools: [], ts: e.timestamp };
      byTurn.set(tid, entry);
      timeline.push(entry);
    }
    const entry = byTurn.get(tid)!;
    if (e.event === "assistant_item_added") entry.text = e.text;
    if (e.event === "stale_result_discarded") {
      entry.tools.push({ source: e.source, status: "discarded", chunks: e.chunks_yielded_before_discard });
    }
    // Audio-level fence: frames stopped mid-utterance, which the LLM-level
    // fence cannot do on its own.
    if (e.event === "stale_audio_discarded") {
      entry.tools.push({ source: "tts_node", status: "discarded", chunks: e.frames_emitted_before_discard });
    }
    if (e.event === "utterance_routed") {
      entry.intent = e.intent;
      entry.workKept = e.work_kept;
    }
    if (e.event === "first_audio_frame") entry.ttfaMs = e.ttfa_ms;
    if (e.event === "history_reconciled") entry.heardFraction = e.played_fraction;
  }

  // derive metrics
  const audioStopLatencies: number[] = [];
  let interruptTs: number | null = null;
  let staleDiscarded = 0;
  let staleLeaked = 0; // stays 0 unless you wire a "leak" event
  for (const e of events) {
    if (e.event === "interrupt_detected") interruptTs = e.timestamp;
    if (e.event === "audio_stopped" && interruptTs !== null) {
      audioStopLatencies.push(e.timestamp - interruptTs);
      interruptTs = null;
    }
    if (e.event === "stale_result_discarded" || e.event === "stale_audio_discarded") staleDiscarded++;
    if (e.event === "stale_result_leaked" || e.event === "STALE_RESULT_LEAKED") staleLeaked++;
  }

  // Perceived response time: end-of-speech to first Rime audio frame.
  const ttfas: number[] = [];
  // Continuity: how often an utterance during tool work kept that work alive
  // instead of throwing it away.
  let workKept = 0;
  let workSuperseded = 0;
  for (const e of events) {
    if (e.event === "first_audio_frame" && typeof e.ttfa_ms === "number") ttfas.push(e.ttfa_ms);
    if (e.event === "utterance_routed") {
      if (e.work_kept) workKept++;
      else workSuperseded++;
    }
  }
  const avg = (a: number[]) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);

  return {
    connected,
    events,
    currentTurn,
    status,
    interruptFlash,
    timeline,
    metrics: {
      avgAudioStopLatencyMs: avg(audioStopLatencies) * 1000,
      staleDiscarded,
      staleLeaked,
      // TTFA is reported as p50/p95 rather than a mean: the tail is what a
      // caller actually notices, and a mean hides it.
      ttfaP50Ms: percentile(ttfas, 0.5),
      ttfaP95Ms: percentile(ttfas, 0.95),
      ttfaSamples: ttfas.length,
      workKept,
      workSuperseded,
    },
  };
}
