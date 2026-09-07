"use client";
import { useMetricsSocket } from "./useMetricsSocket";

export default function ObservabilityPanel() {
  const { connected, currentTurn, status, interruptFlash, timeline, metrics } =
    useMetricsSocket();

  return (
    <div style={{ display: "grid", gap: 16, fontFamily: "monospace" }}>
      {/* A: status */}
      <div
        style={{
          padding: 12,
          borderRadius: 8,
          background: interruptFlash ? "#ff4444" : "#1a1a1a",
          color: "white",
          transition: "background 0.15s",
        }}
      >
        <b>Turn {currentTurn ?? "–"}</b> · {connected ? "connected" : "disconnected"} ·{" "}
        <span style={{ textTransform: "uppercase" }}>{status}</span>
      </div>

      {/* B: timeline */}
      <div style={{ maxHeight: 300, overflowY: "auto", border: "1px solid #333", padding: 8 }}>
        {timeline.map((t) => (
          <div key={t.turn_id} style={{ marginBottom: 8, paddingBottom: 8, borderBottom: "1px solid #222" }}>
            <div><b>Turn {t.turn_id}</b> — {new Date(t.ts * 1000).toLocaleTimeString()}</div>
            {t.text && <div>💬 {t.text}</div>}
            {t.tools.map((tool, i) => (
              <div key={i} style={{ color: tool.status === "discarded" ? "#ff6666" : "#66ff66" }}>
                🔧 {tool.source ?? "tool"} — {tool.status}
                {tool.chunks != null && ` (${tool.chunks} chunks before discard)`}
              </div>
            ))}
          </div>
        ))}
      </div>

      {/* C: metrics */}
      <div style={{ display: "flex", gap: 24 }}>
        <div>Avg audio-stop latency: <b>{metrics.avgAudioStopLatencyMs.toFixed(0)}ms</b></div>
        <div>Stale discarded: <b>{metrics.staleDiscarded}</b></div>
        <div>Stale leaked: <b style={{ color: metrics.staleLeaked ? "red" : undefined }}>{metrics.staleLeaked}</b></div>
      </div>
    </div>
  );
}
