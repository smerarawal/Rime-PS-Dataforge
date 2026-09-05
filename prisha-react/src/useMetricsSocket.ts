"use client";
import { useEffect, useRef, useState } from "react";

export type MetricEvent = { event: string; timestamp: number; [k: string]: any };

type TurnEntry = {
  turn_id: number;
  text?: string;
  tools: { source?: string; status: "pending" | "discarded"; chunks?: number }[];
  ts: number;
};

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
    if (e.event === "stale_result_discarded") staleDiscarded++;
    if (e.event === "stale_result_leaked") staleLeaked++;
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
    },
  };
}
