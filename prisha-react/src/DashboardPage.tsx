
import { useState } from "react";
import { useMetricsSocket } from "./useMetricsSocket";

const c = {
  ink: "#111214",
  muted: "#6B7280",
  card: "#FFFFFF",
  green: "#2E8B57",
  pink: "#E23F6E",
  border: "#EAECEE",
};

const TABS = ["Overview", "Timeline", "Metrics", "Connection"] as const;

export default function DashboardPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Overview");
  const { connected, currentTurn, status, interruptFlash, timeline, metrics } =
    useMetricsSocket();

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "linear-gradient(180deg, #BFE0F0 0%, #E8F3F8 45%, #F5F7F5 100%)",
        fontFamily: "Inter, system-ui, sans-serif",
        color: c.ink,
        padding: "24px 24px 60px",
      }}
    >
      {/* pill nav */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", maxWidth: 1000, margin: "0 auto 40px" }}>
        <div style={{ fontWeight: 600 }}>Turn Fence</div>
        <div style={{ display: "flex", gap: 4, background: "#fff", borderRadius: 999, padding: 6, boxShadow: "0 8px 24px rgba(0,0,0,0.06)" }}>
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              style={{
                border: "none",
                borderRadius: 999,
                padding: "8px 16px",
                fontSize: 13,
                fontWeight: 500,
                cursor: "pointer",
                background: tab === t ? c.ink : "transparent",
                color: tab === t ? "#fff" : c.muted,
              }}
            >
              {t}
            </button>
          ))}
        </div>
        <div
          style={{
            padding: "8px 16px",
            borderRadius: 999,
            background: c.ink,
            color: "#fff",
            fontSize: 13,
            fontWeight: 500,
          }}
        >
          {connected ? "Live" : "Reconnecting…"}
        </div>
      </div>

      {/* floating card */}
      <div
        style={{
          maxWidth: 1000,
          margin: "0 auto",
          background: c.card,
          borderRadius: 24,
          boxShadow: "0 20px 60px rgba(0,0,0,0.08)",
          padding: 40,
          minHeight: 420,
        }}
      >
        {tab === "Overview" && (
          <div style={{ display: "flex", alignItems: "center", gap: 48 }}>
            <div>
              <div style={{ fontSize: 13, color: c.muted, marginBottom: 8 }}>Current turn</div>
              <div
                style={{
                  fontFamily: "'Playfair Display', Georgia, serif",
                  fontSize: 96,
                  lineHeight: 1,
                  transition: "color 0.2s",
                  color: interruptFlash ? c.pink : c.ink,
                }}
              >
                {currentTurn ?? "–"}
              </div>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div style={{ fontSize: 13, color: c.muted }}>Agent state</div>
              <div
                style={{
                  fontSize: 20,
                  fontWeight: 600,
                  color: status === "interrupted" ? c.pink : status === "speaking" ? c.green : c.ink,
                  textTransform: "capitalize",
                }}
              >
                {status}
              </div>
            </div>
          </div>
        )}

        {tab === "Timeline" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 0, maxHeight: 380, overflowY: "auto" }}>
            {timeline.map((t, i) => (
              <div
                key={t.turn_id}
                style={{
                  display: "flex",
                  gap: 16,
                  padding: "16px 0",
                  borderBottom: i < timeline.length - 1 ? `1px solid ${c.border}` : "none",
                }}
              >
                <div style={{ width: 32, color: c.muted, fontVariantNumeric: "tabular-nums" }}>{t.turn_id}</div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 12, color: c.muted, marginBottom: 4 }}>
                    {new Date(t.ts * 1000).toLocaleTimeString()}
                  </div>
                  {t.text && <div style={{ fontSize: 15 }}>{t.text}</div>}
                  {t.tools.map((tool, j) => (
                    <div key={j} style={{ marginTop: 6, fontSize: 13, color: tool.status === "discarded" ? c.pink : c.green }}>
                      {tool.source ?? "tool"} — {tool.status}
                      {tool.chunks != null && ` · ${tool.chunks} chunks discarded`}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {tab === "Metrics" && (
          <div style={{ display: "flex", gap: 48 }}>
            <Stat label="avg audio-stop" value={`${metrics.avgAudioStopLatencyMs.toFixed(0)}ms`} color={c.ink} />
            <Stat label="discarded" value={String(metrics.staleDiscarded)} color={c.green} />
            <Stat label="leaked" value={String(metrics.staleLeaked)} color={metrics.staleLeaked ? c.pink : c.ink} />
          </div>
        )}

        {tab === "Connection" && (
          <div style={{ fontSize: 14, color: c.muted }}>
            WebSocket: <b style={{ color: connected ? c.green : c.pink }}>{connected ? "connected" : "disconnected"}</b>
            <div style={{ marginTop: 8 }}>ws://localhost:8765</div>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div>
      <div style={{ fontSize: 13, color: "#6B7280", marginBottom: 6 }}>{label}</div>
      <div style={{ fontFamily: "'Playfair Display', Georgia, serif", fontSize: 40, color }}>{value}</div>
    </div>
  );
}
