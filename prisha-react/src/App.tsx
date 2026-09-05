import ObservabilityPanel from "./ObservabilityPanel";
import "./App.css";

function App() {
  return (
    <main
      style={{
        minHeight: "100vh",
        padding: "32px",
        background: "#0d0d0d",
        color: "white",
        boxSizing: "border-box",
      }}
    >
      <div
        style={{
          maxWidth: "1100px",
          margin: "0 auto",
        }}
      >
        <h1>Voice Agent Observability</h1>

        <p style={{ color: "#aaa", marginBottom: "24px" }}>
          Realtime status, request timeline and interruption metrics
        </p>

        <ObservabilityPanel />
      </div>
    </main>
  );
}

export default App;