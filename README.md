# Rime PS — DataForge

## Overview

DataForge is a voice-native customer support agent designed for reliable conversational interaction under interruption. The application combines real-time voice processing with turn-aware observability to make agent behavior measurable during live conversations.

## Observability

The project includes a real-time observability interface for monitoring the voice agent while it is running.

### Observability Panel

`ObservabilityPanel.tsx` provides a live dashboard showing:

- Current conversation turn
- Agent connection status
- Current interaction state
- Interruption events
- Audio-stop latency
- Stale tool results discarded
- Stale results leaked
- Turn-by-turn event timeline
- Assistant responses associated with each turn

The panel is designed to make interruption and recovery behavior visible during testing.

### Metrics WebSocket

`useMetricsSocket.ts` provides the frontend WebSocket client.

It:

- Connects to the local metrics WebSocket endpoint
- Receives real-time agent events
- Maintains the event stream in React state
- Tracks the active turn
- Detects interruption events
- Calculates audio-stop latency
- Counts discarded stale results
- Detects stale-result leakage
- Builds a turn-based timeline for the observability panel

### WebSocket Bridge

`metrics_ws_bridge.py` exposes the backend metrics stream to the frontend through a WebSocket connection.

The bridge:

1. Reads events from `MetricsLog`
2. Detects newly recorded events
3. Sends them to connected WebSocket clients
4. Keeps the observability dashboard synchronized with the running agent

The default endpoint is:

```text
ws://localhost:8765
```

## Agent Integration

`agent.py` integrates the metrics WebSocket bridge into the LiveKit agent process.

The agent starts the bridge alongside the voice agent so that runtime events can be observed by the frontend without requiring a separate metrics process.

The integration also supports interruption-aware turn handling and records events such as:

- `interrupt_detected`
- `audio_stopped`
- `stale_result_discarded`
- `assistant_item_added`

## Metrics

`metrics.py` provides the in-memory event logging layer used by the agent and observability system.

Each event contains a timestamp and relevant turn information. The metrics layer supports:

- Recording runtime events
- Retrieving recorded events
- Exporting metrics as JSON
- Resetting recorded events
- Measuring intervals between selected events

These metrics provide the data used by the observability dashboard and acceptance testing.

## React Application

The frontend is implemented using React and TypeScript.

### Components

```text
prisha-react/
└── src/
    ├── App.tsx
    ├── App.css
    ├── index.css
    ├── main.tsx
    ├── ObservabilityPanel.tsx
    └── useMetricsSocket.ts
```

### `App.tsx`

Provides the main application entry point and renders the observability interface.

### `ObservabilityPanel.tsx`

Provides the visual monitoring dashboard for live agent behavior and metrics.

### `useMetricsSocket.ts`

Provides the React hook responsible for receiving and processing live backend metrics.

## Interruption and Turn Observability

The observability system is focused on the agent's behavior when a user interrupts an ongoing response.

The runtime flow is:

```text
User interruption
       ↓
interrupt_detected
       ↓
New turn created
       ↓
Active tool work cancelled
       ↓
Agent playback interrupted
       ↓
audio_stopped
       ↓
New response continues on the latest turn
```

Stale work is tracked separately so that results belonging to an older turn can be identified and discarded instead of being surfaced to the user.

## Runtime Flow

```text
LiveKit Voice Agent
        │
        ├── TurnManager
        ├── Tools
        └── MetricsLog
                │
                ▼
        metrics_ws_bridge.py
                │
                │ WebSocket
                ▼
        React Frontend
                │
                ▼
     ObservabilityPanel
```

## Running the Application

### Backend

Install the required Python dependencies and configure the required environment variables.

Then start the LiveKit agent:

```bash
python agent.py dev
```

The metrics WebSocket bridge runs with the agent and exposes:

```text
ws://localhost:8765
```

### Frontend

From the React application directory:

```bash
npm install
npm run dev
```

Open the local development URL displayed by Vite.

## Testing

Turn-management behavior can be tested with:

```bash
python -m pytest test_turn_manager.py -v
```

The acceptance test can be run with:

```bash
python acceptance_test.py
```

For interruption stress testing, the tool delay can be increased using:

```bash
STRESS_TEST_TOOL_DELAY_MS=4000
```

On Windows PowerShell:

```powershell
$env:STRESS_TEST_TOOL_DELAY_MS="4000"
python agent.py dev
```

## Key Metrics

The observability layer focuses on three important outcomes:

### Audio-stop latency

Measures the time between interruption detection and the recorded audio-stop event.

### Stale results discarded

Counts results generated for an outdated conversation turn that were correctly rejected.

### Stale results leaked

Tracks cases where an outdated result incorrectly reaches the active conversation.

These metrics make interruption correctness measurable rather than relying only on subjective audio testing.

## Configuration

Runtime configuration is supplied through environment variables. API keys and credentials should be stored in a local `.env` file and should never be committed to the repository.

Typical services used by the voice pipeline include:

- LiveKit
- Rime TTS
- Deepgram STT
- Groq/OpenAI-compatible LLM

## Project Structure

```text
.
├── agent.py
├── metrics.py
├── metrics_ws_bridge.py
├── turn_manager.py
├── tools.py
├── acceptance_test.py
├── test_turn_manager.py
└── prisha-react/
    └── src/
        ├── App.tsx
        ├── App.css
        ├── index.css
        ├── main.tsx
        ├── ObservabilityPanel.tsx
        └── useMetricsSocket.ts
```

## Development Notes

The observability interface is intended for local development and evaluation of real-time voice-agent behavior. The WebSocket metrics stream provides immediate visibility into turn transitions, interruptions, stale-result handling, and response activity while the agent is running.
