# CLI Runner Architecture Specification

`cli-runner` acts as a host-side execution gateway that bridges containerized applications (Docker/Kubernetes) with subscription-authenticated AI coding agent CLIs (such as Command Code `cmd` and Antigravity `agy`).

---

## 1. System Overview

```
┌────────────────────────────────────────────────────────┐
│               Containerized Application                │
│             (Backend, Web App, Batch Jobs)             │
└───────────────────────────┬────────────────────────────┘
                            │ HTTP (REST / SSE)
                            │ Header: X-CLI-Token
                            ▼
┌────────────────────────────────────────────────────────┐
│                       CLI Runner                       │
│ ┌────────────────────────────────────────────────────┐ │
│ │ Ingress & Security                                 │ │
│ │ • Constant-time Token Auth (secrets.compare_digest)│ │
│ │ • IP / CIDR Allowlist Verification                 │ │
│ └─────────────────────────┬──────────────────────────┘ │
│                           ▼                            │
│ ┌────────────────────────────────────────────────────┐ │
│ │ Agent Router                                       │ │
│ │ • Task Classification (speed / reasoning / cost)   │ │
│ │ • Provider & Effort Mapping (cmd vs. agy)          │ │
│ └──────────────┬──────────────────────┬──────────────┘ │
│                │ Synchronous / SSE    │ Asynchronous   │
│                ▼                      ▼                │
│ ┌───────────────────────────┐ ┌──────────────────────┐ │
│ │ Concurrency Semaphore     │ │ Async Job Queue      │ │
│ │ (bounded parallelism)     │ │ (Worker Pool & Ring) │ │
│ └──────────────┬────────────┘ └──────────┬───────────┘ │
│                └──────────────┬──────────┘             │
│                               ▼                        │
│ ┌────────────────────────────────────────────────────┐ │
│ │ Execution Engine & Process Sandbox                 │ │
│ │ • Isolated Run Subdirectory (per-job cwd)          │ │
│ │ • Sandboxed $HOME & Credential Mirroring           │ │
│ │ • Cross-Platform Process Launcher (Linux/macOS/Win)│ │
│ │ • Real-time Stdout SSE Streamer                    │ │
│ │ • Automatic Lifecycle Cleanup                      │ │
│ └─────────────────────────┬──────────────────────────┘ │
│                           ▼                            │
│ ┌────────────────────────────────────────────────────┐ │
│ │ Observability & Telemetry                          │ │
│ │ • Live In-Flight & Queue Depth Gauges              │ │
│ │ • Prometheus /metrics Exposition (Counters/Sums)   │ │
│ └────────────────────────────────────────────────────┘ │
└───────────────────────────┬────────────────────────────┘
                            │ Subprocess Spawn (argv)
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

---

## 2. Core Architectural Pillars

### 2.1 Process & Filesystem Isolation
Coding agents explore directories, write scratch files, and compile dependencies. Running agents in a shared working directory or against the main application folder introduces race conditions and cross-agent file pollution.

- **Workspace Per Run**: Every execution creates a unique temporary directory under `CLI_WORKSPACE_DIR` (`run-XXXXXX`). The agent process runs with `cwd` locked to this isolated directory.
- **Credential Mirroring**: For tools like Command Code that rely on `~/.commandcode`, an isolated `HOME` is generated per run on POSIX systems with mirrored read-only credentials.
- **Deterministic Cleanup**: On process completion, timeout, or cancellation, the scratch workspace is removed via `shutil.rmtree`.

### 2.2 Fresh Sessions vs. Continuation
In batch processing environments, conversation continuation flags (`-c` or `--continue`) can cause agents to produce conversational summaries instead of structured JSON outputs. `cli-runner` enforces fresh invocation semantics per prompt to maintain structured parsing reliability.

### 2.3 Controlled Concurrency & Backpressure
Serial execution turns multi-agent workflows into severe bottlenecks. Unbounded concurrency exhausts machine memory and triggers subscription session rate-limits.
- A configurable `asyncio.Semaphore` (`CLI_MAX_CONCURRENCY`, default 5) gates active subprocesses.
- Queued requests are tracked via atomic gauges (`_waiting`) and reflected in `/health` and `/metrics`.

### 2.4 Dual Execution Modes

| Mode | Endpoint | Description |
|---|---|---|
| **Synchronous** | `POST /run` | Direct HTTP request-response. Blocks until agent completes and returns JSON. |
| **Streaming** | `POST /run/stream` | Server-Sent Events (`text/event-stream`). Emits line-by-line stdout chunks in real-time. |
| **Asynchronous** | `POST /jobs` | Queues execution into background worker pool. Immediate `202 Accepted` with queryable job ID. |

### 2.5 Dynamic Agent Router
Clients can either specify an explicit provider (`cmdcode` or `agy`) or use `provider="auto"`:
```json
{
  "provider": "auto",
  "task": "code_refactoring",
  "prompt": "Refactor database query layer",
  "requirements": {
    "reasoning": "high",
    "speed": "medium"
  }
}
```
The router evaluates task complexity, speed, and cost preferences to select the optimal CLI and `--effort` parameter (`low`, `medium`, `high`).

### 2.6 Cross-Platform Daemons
- **Linux**: systemd unit (`cli-runner.service`) running under host user privileges.
- **macOS**: launchd agent (`com.cli-runner.plist`) with `RunAtLoad` and `KeepAlive`.
- **Windows**: Scheduled Task via PowerShell script (`install-windows-service.ps1`) executing `cmd.cmd` shims without name collisions with `cmd.exe`.
