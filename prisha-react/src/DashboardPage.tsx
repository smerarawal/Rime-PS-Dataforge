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
const serif = "'Playfair Display', Georgia, serif";

const TABS = [
  {
    key: "Overview",
    label: "Overview",
    blurb: "Every user utterance gets a monotonically increasing turn id. Async work is stamped with the turn active when it started, and re-checked right before it's spoken.",
  },
  {
    key: "Timeline",
    label: "Timeline",
    blurb: "A sequential record of every turn: what was said, which tools ran, and whether their results were used or discarded as stale.",
  },
  {
    key: "Metrics",
    label: "Metrics",
    blurb: "Audio-stop latency per interrupt, and a running count of stale results correctly blocked vs. leaked.",
  },
  {
    key: "Connection",
    label: "Connection",
    blurb: "Live status of the WebSocket bridge between the voice agent backend and this dashboard.",
  },
] as const;

export default function DashboardPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("Overview");
  const { connected, currentTurn, status, interruptFlash, timeline, metrics } =
    useMetricsSocket();
  const active = TABS.find((t) => t.key === tab)!;
  const recent = timeline.slice(-4);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "linear-gradient(180deg, #BFE0F0 0%, #E8F3F8 45%, #F5F7F5 100%)",
        fontFamily: "Inter, system-ui, sans-serif",
        color: c.ink,
      }}
    >
      <div style={{ maxWidth: 1160, margin: "0 auto", padding: "28px 28px 80px" }}>
        {/* hero */}
        <div style={{ textAlign: "center", marginBottom: 40 }}>

          <div style={{ fontFamily: serif, fontSize: 56, lineHeight: 1.15, maxWidth: 760, margin: "0 auto" }}>
            Never let a stale reply reach the user
          </div>
          <div style={{ color: c.muted, marginTop: 12, fontSize: 16 }}>
            Live turn-fencing evidence for the voice agent, visible in real time.
          </div>
        </div>

        {/* product-shot card */}
        <div
          style={{
            background: c.card, borderRadius: 24, boxShadow: "0 20px 60px rgba(0,0,0,0.1)",
            padding: 32, display: "grid", gridTemplateColumns: "220px 1fr", gap: 24, marginBottom: 44,
          }}
        >
          {/* stat grid */}
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            <div>
              <div style={{ fontSize: 12, color: c.muted }}>Current turn</div>
              <div style={{ fontFamily: serif, fontSize: 40, color: interruptFlash ? c.pink : c.ink }}>
                {currentTurn ?? "0"}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: c.muted }}>State</div>
              <div style={{ fontSize: 16, fontWeight: 600, textTransform: "capitalize", color: status === "interrupted" ? c.pink : status === "speaking" ? c.green : c.ink }}>
                {status}
              </div>
            </div>
            <div style={{ display: "flex", gap: 16 }}>
              <MiniStat label="blocked" value={metrics.staleDiscarded} color={c.green} />
              <MiniStat label="leaked" value={metrics.staleLeaked} color={metrics.staleLeaked ? c.pink : c.ink} />
            </div>
          </div>

          {/* chat-bubble style recent activity */}
          <div style={{ background: "#F7F8F9", borderRadius: 16, padding: 20, display: "flex", flexDirection: "column", gap: 10, minHeight: 200, justifyContent: recent.length ? "flex-start" : "center" }}>
            {recent.length === 0 && (
              <div style={{ textAlign: "center", color: c.muted, fontSize: 13 }}>
                Waiting for events — start ws_server.py (or agent.py) to see live turns here.
              </div>
            )}
            {recent.map((t) => (
              <div key={t.turn_id} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                <div style={{ width: 22, height: 22, borderRadius: "50%", background: c.ink, color: "#fff", fontSize: 11, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                  {t.turn_id}
                </div>
                <div style={{ background: "#fff", borderRadius: 10, padding: "8px 12px", fontSize: 13, boxShadow: "0 2px 6px rgba(0,0,0,0.04)" }}>
                  {t.text ?? "…"}
                  {t.tools.map((tool, j) => (
                    <div key={j} style={{ marginTop: 4, fontSize: 11, color: tool.status === "discarded" ? c.pink : c.green }}>
                      {tool.source ?? "tool"} · {tool.status}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* two-column feature section, tab-driven */}
        <div style={{ display: "grid", gridTemplateColumns: "250px 1fr", gap: 28 }}>
          <div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 24 }}>
              {TABS.map((t) => (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  style={{
                    textAlign: "left", border: "none", background: "transparent", cursor: "pointer",
                    padding: "10px 0", fontSize: 14, fontWeight: tab === t.key ? 600 : 400,
                    color: tab === t.key ? c.ink : c.muted, borderBottom: `1px solid ${c.border}`,
                  }}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <div style={{ fontFamily: serif, fontSize: 22, marginBottom: 8 }}>{active.label}</div>
            <div style={{ fontSize: 14, color: c.muted, lineHeight: 1.6 }}>{active.blurb}</div>
          </div>

          <div style={{ background: c.card, borderRadius: 20, boxShadow: "0 16px 40px rgba(0,0,0,0.06)", padding: 28, minHeight: 340 }}>
            {tab === "Overview" && (
              <div style={{ fontSize: 14, color: c.muted }}>
                {timeline.length} turns observed since connecting.
                {status === "interrupted" && <div style={{ marginTop: 12, color: c.pink, fontWeight: 600 }}>⚠ Interrupt in progress</div>}
              </div>
            )}
            {tab === "Timeline" && (
              <div style={{ display: "flex", flexDirection: "column", maxHeight: 340, overflowY: "auto" }}>
                {timeline.map((t, i) => (
                  <div key={t.turn_id} style={{ display: "flex", gap: 16, padding: "14px 0", borderBottom: i < timeline.length - 1 ? `1px solid ${c.border}` : "none" }}>
                    <div style={{ width: 28, color: c.muted, fontVariantNumeric: "tabular-nums" }}>{t.turn_id}</div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 11, color: c.muted, marginBottom: 4 }}>{new Date(t.ts * 1000).toLocaleTimeString()}</div>
                      {t.text && <div style={{ fontSize: 14 }}>{t.text}</div>}
                      {t.tools.map((tool, j) => (
                        <div key={j} style={{ marginTop: 6, fontSize: 12, color: tool.status === "discarded" ? c.pink : c.green }}>
                          {tool.source ?? "tool"} — {tool.status}{tool.chunks != null && ` · ${tool.chunks} chunks discarded`}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
                {timeline.length === 0 && <div style={{ color: c.muted, fontSize: 13 }}>No turns yet.</div>}
              </div>
            )}
            {tab === "Metrics" && (
              <div style={{ display: "flex", gap: 40 }}>
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
      </div>
    </div>
  );
}

function MiniStat({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "#6B7280" }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 600, color }}>{value}</div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div>
      <div style={{ fontSize: 13, color: "#6B7280", marginBottom: 6 }}>{label}</div>
      <div style={{ fontFamily: serif, fontSize: 40, color }}>{value}</div>
    </div>
  );
}
