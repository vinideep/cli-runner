# CLI Runner

**High-Throughput Execution Gateway for Subscription-Based AI Coding Agents**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Prometheus Ready](https://img.shields.io/badge/metrics-Prometheus-orange.svg)](docs/architecture.md)

---

## The Thesis: Execution Economics for AI Applications

The default architecture for Generative AI applications follows a predictable path:
```
Application ──► LLM API ──► Pay Per Token ──► Unsustainable Infrastructure Bill
```

While metered token pricing works for simple single-turn completions, **autonomous coding agents operate differently**:
- They ingest thousands of lines of repository context across multi-file dependencies.
- They iterate across 5–15 tool turns (reading files, executing bash commands, running tests, fixing errors).
- Conversation history and context resubmission cause token usage to inflate exponentially ($O(N^2)$).

A few dozen background agent tasks can quickly turn into hundreds of millions of tokens.

**CLI Runner shifts the paradigm:**
Instead of asking *"How many tokens can I afford?"*, you ask *"How much useful work can I extract from an agent subscription?"*

By running a host-side execution gateway, containerized applications can dispatch prompts directly to authenticated CLI agents (such as **Command Code (`cmd`)** and **Antigravity (`agy`)**) utilizing subscription tiers with included quotas rather than variable per-token metering.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Docker / Backend Application                │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / SSE (X-CLI-Token)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                         CLI RUNNER                          │
│                                                             │
│   ┌───────────────────────┐     ┌───────────────────────┐   │
│   │ Security & Auth       │     │ Agent Router          │   │
│   │ • Constant-time Token │     │ • Speed / Reasoning   │   │
│   │ • IP / CIDR Filter    │     │ • Auto CLI Selection  │   │
│   └───────────┬───────────┘     └───────────┬───────────┘   │
│               │                             │               │
│               ▼                             ▼               │
│   ┌───────────────────────┐     ┌───────────────────────┐   │
│   │ Execution Modes       │     │ Concurrency Control   │   │
│   │ • POST /run (Sync)    │     │ • Async Semaphore     │   │
│   │ • POST /run/stream    │     │ • Live Queue Depth    │   │
│   │ • POST /jobs (Async)  │     │ • Prometheus Metrics  │   │
│   └───────────┬───────────┘     └───────────┬───────────┘   │
│               └──────────────┬──────────────┘               │
│                              ▼                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ Sandboxed Workspace Engine                          │   │
│   │ • Ephemeral Run Directory (`run-XXXXXX`)             │   │
│   │ • Mirrored Isolated $HOME (.commandcode)            │   │
│   │ • Subprocess Lifecycle & Deterministic Cleanup      │   │
│   └──────────────────────────┬──────────────────────────┘   │
└──────────────────────────────┼──────────────────────────────┘
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
      ┌───────────────────┐         ┌───────────────────┐
      │ Command Code CLI  │         │  Antigravity CLI  │
      │      (`cmd`)      │         │      (`agy`)      │
      └─────────┬─────────┘         └─────────┬─────────┘
                │ Host Subscription           │ Quota Tier
                ▼                             ▼
          AI Agent Core                 AI Agent Core
```

For detailed technical deep-dives:
- [Architecture Specification](docs/architecture.md)
- [Inference Economics & Cost Modeling](docs/economics.md)
- [Benchmarks & Performance Analysis](docs/benchmarks.md)

---

## Features

- **Dual Execution Models**:
  - **Synchronous (`POST /run`)**: Clean, predictable JSON response.
  - **Real-Time Streaming (`POST /run/stream`)**: Server-Sent Events (SSE) streaming process stdout line-by-line.
  - **Asynchronous Job Queue (`POST /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}`)**: Background worker pool with status tracking and cancellation.
- **Dynamic Agent Router (`POST /route` or `provider="auto"`)**: Automatically resolves optimal CLI and reasoning effort based on task speed, reasoning depth, and cost requirements.
- **Strict Filesystem Isolation**: Every concurrent run executes inside a freshly allocated, isolated workspace directory (`run-XXXXXX`), cleaned up upon completion.
- **Process Concurrency Control**: Configurable `asyncio.Semaphore` prevents host process saturation and respects subscription session thresholds.
- **Zero Conversation Reuse**: Enforces clean sessions per prompt, preventing conversational state drift and ensuring structured JSON responses.
- **Enterprise Security**:
  - Constant-time token verification (`secrets.compare_digest`).
  - CIDR/IP allowlist filtering (`CLI_ALLOWED_IPS`).
  - Max execution timeouts and output truncation guards.
- **Observability**: Exposes standard **Prometheus metrics (`/metrics`)** and live queue depth gauges (`/health`). Authenticated `/ready` proves the backend's shared token matches before any CLI prompt is launched.
- **Cross-Platform Host Daemons**: Native auto-restart and boot scripts for Linux (`systemd`), macOS (`launchd`), and Windows (`Task Scheduler`).

---

## Quickstart

### 1. Installation

```bash
git clone https://github.com/vinideep/cli-runner.git
cd cli-runner
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

For the Linux production unit, install the repository at
`/home/deepi/app/cli-runner` (or update the paths in the unit before copying
it). Create `/etc/cli-runner/cli-runner.env` as root with mode `0600` and set
`CLI_TOKEN` to the same value as the backend `HOST_CLI_TOKEN`; keep this file
outside Git and never print it in diagnostics. The unit loads that file via
`EnvironmentFile`, so a restart cannot revert the token to a committed
placeholder.

Ensure your CLIs are authenticated on the host:
```bash
cmd status    # Command Code CLI
agy --help    # Antigravity CLI
```

### 2. Configure Environment

```bash
export CLI_TOKEN="your-secure-shared-secret"
export CLI_MAX_CONCURRENCY=5
```

### 3. Run

```bash
python main.py
```

---

## API Endpoints

### `GET /ready` (Authenticated Readiness)
Prove the caller and runner share the same `CLI_TOKEN` without launching a CLI
process or consuming a subscription turn:

```bash
curl -s http://127.0.0.1:8899/ready \
  -H "X-CLI-Token: $CLI_TOKEN" | jq
```

### `POST /run` (Synchronous Execution)
Execute an agent prompt and await the structured result.

```bash
curl -s -X POST http://127.0.0.1:8899/run \
  -H "X-CLI-Token: $CLI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "cmdcode",
    "prompt": "Return only a JSON array with all prime numbers under 20",
    "effort": "low"
  }' | jq
```

### `POST /run/stream` (Real-Time SSE Streaming)
Stream output chunks in real time as Server-Sent Events:

```bash
curl -N -X POST http://127.0.0.1:8899/run/stream \
  -H "X-CLI-Token: $CLI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "agy",
    "prompt": "Analyze repository security posture"
  }'
```

Output:
```
event: queued
data: {"stream_id": "stream-1", "provider": "agy"}

event: start
data: {"stream_id": "stream-1", "provider": "agy", "effort": null}

event: chunk
data: {"chunk": "Scanning file tree...\n"}

event: done
data: {"ok": true, "exit_code": 0, "latency_ms": 1420}
```

### `POST /jobs` (Asynchronous Job Execution)
Queue an agent task in the background worker pool:

```bash
# Submit job
curl -s -X POST http://127.0.0.1:8899/jobs \
  -H "X-CLI-Token: $CLI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider": "cmdcode", "prompt": "Refactor legacy modules"}' | jq

# Check status
curl -s http://127.0.0.1:8899/jobs/job-38a4d79e8c1b \
  -H "X-CLI-Token: $CLI_TOKEN" | jq

# Cancel job
curl -s -X DELETE http://127.0.0.1:8899/jobs/job-38a4d79e8c1b \
  -H "X-CLI-Token: $CLI_TOKEN" | jq
```

### `POST /route` (Agent Router Recommendation)
Inspect intelligent agent routing recommendations:

```bash
curl -s -X POST http://127.0.0.1:8899/route \
  -H "X-CLI-Token: $CLI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "syntax_fix",
    "requirements": {"speed": "high", "cost": "low"}
  }' | jq
```

### `GET /metrics` (Prometheus Telemetry)
Scrape operational telemetry:
```bash
curl -s http://127.0.0.1:8899/metrics
```

Output:
```text
# HELP cli_runner_requests_total Total number of agent execution requests.
# TYPE cli_runner_requests_total counter
cli_runner_requests_total{provider="cmd",status="success"} 42

# HELP cli_runner_active_processes Current number of active CLI processes.
# TYPE cli_runner_active_processes gauge
cli_runner_active_processes 2

# HELP cli_runner_waiting_requests Current number of requests queued behind semaphore.
# TYPE cli_runner_waiting_requests gauge
cli_runner_waiting_requests 0
```

---

## Production Host Daemons

### Linux — systemd
```bash
sudo cp cli-runner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cli-runner
```

### macOS — launchd
```bash
cp cli-runner.plist ~/Library/LaunchAgents/com.cli-runner.plist
launchctl load -w ~/Library/LaunchAgents/com.cli-runner.plist
```

### Windows — Task Scheduler
```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows-service.ps1
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `CLI_TOKEN` | *(Required)* | Secret token expected in `X-CLI-Token` header. |
| `CLI_ALLOWED_IPS` | `""` | Comma-separated list of allowed IPs / CIDRs (e.g. `127.0.0.1,172.17.0.0/16`). |
| `CLI_MAX_CONCURRENCY` | `5` | Maximum concurrent agent CLI processes. |
| `CLI_WORKSPACE_DIR` | OS-specific | Base path for isolated scratch directories. |
| `CLI_DEFAULT_TIMEOUT` | `1800` | Per-task execution timeout in seconds. |
| `CMD_MAX_TURNS` | `10` | Maximum agent conversational turns (`--max-turns`). |
| `CLI_MAX_OUTPUT_CHARS`| `1000000` | Truncation ceiling for subprocess stdout. |
| `CLI_BIND_HOST` | `0.0.0.0` | Host bind address (requires firewalling in production). |
| `CLI_PORT` | `8899` | Gateway listening port. |

---

## License

MIT License. See [LICENSE](LICENSE) for details.
