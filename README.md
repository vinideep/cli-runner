# cli-runner

A tiny host-side HTTP bridge that runs the **Command Code (`cmd`)** and
**Antigravity (`agy`)** CLIs. It replaces the old host-side async job queue +
cron-worker setup.

The Dockerized backend can't run these CLIs (their auth lives on the host under
`/root`). So this service runs **directly on the host** (systemd, not Docker) and
the backend calls it synchronously per prompt. No queue, no cron, no polling.

## Cross-platform: just run `python main.py`

The OS is auto-detected at startup and everything configures itself:

| OS | What happens automatically |
|----|---------------------------|
| **Linux** | Defaults: `cmd`, `/root/.local/bin/agy`, `/tmp/cli-runner-workspace`. |
| **macOS** | `cmd` and `agy` are resolved from your `PATH` (falling back to `~/.local/bin/agy`), workspace is `/tmp/cli-runner-workspace`. |
| **Windows** | Uses the npm batch shims `cmd.cmd`/`agy.cmd` (never the built-in `cmd.exe` shell — that's a name collision with the CLI), launches them via `cmd.exe /c`, and puts the workspace under `%LOCALAPPDATA%\cli-runner-workspace`. HOME isolation is skipped because Node ignores `HOME` on Windows and reads `%USERPROFILE%\.commandcode` instead. |

`python main.py` starts uvicorn with the detected settings and prints a startup
banner showing the detected platform, each CLI's install/auth status, workspace,
and bind address. `GET /health` also reports the detected `platform`.

All the environment variables below still work as overrides on every OS.

## Endpoints

- `POST /run` — token-gated (`X-CLI-Token` header).
  Body: `{ "provider": "cmdcode"|"agy", "prompt": "...", "model": "optional",
  "effort": "low|medium|high" (optional), "timeout": 1800 }`.
  Returns: `{ "ok": bool, "response": str|null, "error": str|null, "latency_ms": int }`.
- `GET /health` — liveness + whether the `cmd`/`agy` binaries resolve + the
  effective `max_concurrency`.

Each call runs a **fresh** CLI process — never `-c/--continue` — that's what
made `cmd` reply with a prose summary instead of JSON in production. If the
backend still gets zero parseable JSON from a response, it retries the SAME
prompt with a fresh call (never a continuation) and a sharper reminder, up to
the backend's JSON-retry-attempts setting before giving up on that chunk.

Up to `CLI_MAX_CONCURRENCY` CLI processes run at once (default 3 — see the
concurrency note below). Each run gets its **own** scratch subdirectory under
`CLI_WORKSPACE_DIR` (never the app directory, never shared across concurrent
runs), cleaned up when the run finishes.

### Tuning `CLI_MAX_CONCURRENCY` (speed)

A full batch pass can dispatch 50+ prompts. Serialised one-at-a-time (the old
default of 1), that's the single biggest reason a run took hours instead of
minutes — the backend's own prompt concurrency doesn't help if this service
processes them one at a time anyway. Raise this until you see reliability
problems (CLI errors mentioning concurrent-session limits, auth hiccups,
timeouts) — there is no code-level ceiling here, only whatever your `cmd`/`agy`
account/plan tolerates concurrently. Isolated per-run workspace dirs mean
raising this is safe from a *filesystem* standpoint regardless of how high you
go.

### Reducing per-run latency

Most of each run's wall-clock time is the CLI's model generation — this service
itself adds only milliseconds (process spawn + queue wait). To make individual
runs faster, send these in the request body:

- **`"effort": "low"`** — both CLIs support `--effort low|medium|high`. `low`
  does less reasoning and returns faster; `high` is slower but deeper. This is
  the biggest single lever for prompt-size analysis prompts that don't need
  deep reasoning.
- **`"model"`** — pick a faster/cheaper model (both CLIs support `--model`).
- **Prompt shape matters more than anything**: ask for a single JSON array with
  no explanation, the way the smoke test below does. Long prose instructions
  and "explain your reasoning" both inflate output tokens, which is where most
  of the time goes.

## Install

Prerequisites on every OS: Python 3.9+, the `cmd` / `agy` CLIs installed and
logged in (`cmd login` / whatever the CLI supports) **as the user this service
will run as**, and a shared `CLI_TOKEN` agreed with the backend.

```bash
# 1. Copy this folder to the host, then:
cd cli-runner
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt

# 2. Confirm the CLIs are logged in AS THE SERVICE USER:
cmd status        # or: cmd whoami
agy --help        # confirm agy is present + authed
```

### Start on boot + auto-restart per OS

The goal is the same on every OS: the service starts when the machine boots and
restarts automatically if it crashes. Each OS has its own service manager.

#### Linux — systemd

```bash
# 3. Edit the unit's CLI_TOKEN (must match the backend's HOST_CLI_TOKEN),
#    User=, HOME=, and paths, then:
sudo cp cli-runner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cli-runner     # enable = start on boot
sudo systemctl status cli-runner           # Restart=on-failure = auto-restart on crash
```

#### macOS — launchd

launchd is the macOS equivalent of systemd. The repo ships
`cli-runner.plist` (a LaunchAgent). `RunAtLoad` starts it at login and
`KeepAlive` restarts it whenever the process exits.

```bash
# 3. Edit cli-runner.plist: CLI_TOKEN, WorkingDirectory, and the venv path,
#    then install it for the logged-in user (the CLIs' auth lives in ~):
cp cli-runner.plist ~/Library/LaunchAgents/com.cli-runner.plist
launchctl load -w ~/Library/LaunchAgents/com.cli-runner.plist   # -w = enabled across reboots
launchctl list | grep cli-runner

# Uninstall:
launchctl unload ~/Library/LaunchAgents/com.cli-runner.plist
rm ~/Library/LaunchAgents/com.cli-runner.plist
```

Note: a LaunchAgent starts when the user **logs in** (not at pure boot), because
the CLI credentials live in that user's home.

#### Windows — Task Scheduler

The repo ships `install-windows-service.ps1`, which registers a scheduled task
that starts at logon (or boot) and restarts on failure, without needing admin
rights or NSSM.

```powershell
# 3. Edit the $Env block at the top of install-windows-service.ps1
#    (CLI_TOKEN etc.), then run it from the project folder:
powershell -ExecutionPolicy Bypass -File .\install-windows-service.ps1

# Starts at logon, auto-restarts on crash. Manage it with:
schtasks /query /tn "cli-runner" /v
schtasks /run /tn "cli-runner"          # start it now
schtasks /end /tn "cli-runner"          # stop it
schtasks /delete /tn "cli-runner" /f    # uninstall
```

### Smoke test

```bash
# From the host itself:
curl -s http://127.0.0.1:8899/health | jq
curl -s -X POST http://127.0.0.1:8899/run \
  -H "X-CLI-Token: <same token>" -H "Content-Type: application/json" \
  -d '{"provider":"cmdcode","prompt":"Return only this JSON array: [{\"ok\":true}]"}' | jq
```

## Backend wiring

The backend reaches this service at `HOST_CLI_URL` (e.g.
`http://host.docker.internal:8899`) with `HOST_CLI_TOKEN` (== `CLI_TOKEN` here).
**Both are set from the app's Settings page (stored in the DB), not the backend
`.env`.** Set them once there and they take effect without a redeploy.

The backend `docker-compose.yml` must give the `backend` service
`extra_hosts: ["host.docker.internal:host-gateway"]` so the container can reach
the host.

**This service must bind `0.0.0.0`, not `127.0.0.1`** (see the systemd unit's
`ExecStart`). Docker's host-gateway mechanism means the container reaches this
service via the host's real network interface, not loopback — a service bound
only to `127.0.0.1` is unreachable from inside the container even with the
`extra_hosts` mapping in place. This was the actual cause of "the backend can't
reach cli-runner" the first time this was deployed.

**Security implication of binding `0.0.0.0`:** the service is then reachable
from anywhere that can route to this host's IP, not just the Docker bridge —
`X-CLI-Token` is the *only* access control, there's no loopback boundary behind
it. Firewall port 8899 to the Docker bridge/gateway CIDR only (see the comment
in `cli-runner.service`) so a leaked token alone isn't enough to reach it from
the LAN or the public internet.

## Auth: which OS user runs this matters (the venv does not)

The Python venv only runs uvicorn — it has no bearing on CLI auth. `cmd`/`agy`
store their credentials under the **OS user's home**, and this service launches
them as subprocesses (separate OS processes). So the service MUST run as the same
user that ran `cmd login` / `agy` auth, and `HOME` must point to that user's
home. If you run it as a different user or with a different HOME, the CLIs will
look unauthenticated even though the venv is fine.

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `CLI_TOKEN` | (required) | Shared secret; must equal the backend's `HOST_CLI_TOKEN`. |
| `CLI_WORKSPACE_DIR` | OS-specific | Isolated cwd for the CLI (defaults: `/tmp/cli-runner-workspace` on Linux/macOS, `%LOCALAPPDATA%\cli-runner-workspace` on Windows). |
| `CMD_BIN` | OS-specific | Command Code binary (defaults: `cmd` on Linux/macOS, `cmd.cmd` on Windows). |
| `AGY_BIN` | OS-specific | Antigravity binary (defaults: `/root/.local/bin/agy` on Linux, `~/.local/bin/agy` or PATH on macOS, `agy.cmd` on Windows). |
| `CLI_DEFAULT_TIMEOUT` | `1800` | Per-call timeout (seconds). |
| `CMD_MAX_TURNS` | `10` | `cmd --max-turns`. |
| `CLI_MAX_CONCURRENCY` | `3` | Concurrent CLI processes — see the tuning note above. |
| `CLI_BIND_HOST` | `0.0.0.0` | Bind address for `python main.py` / uvicorn. |
| `CLI_PORT` | `8899` | Port for `python main.py` / uvicorn. |

## Running without a service manager (dev / desktop)

```bash
# Any OS — pip install, then just run it (no uvicorn command needed):
pip install -r requirements.txt
python main.py            # OS auto-detected; startup banner shows what was found

# Set a token before first use (unless you only call /health):
export CLI_TOKEN=dev-secret            # macOS/Linux
set CLI_TOKEN=dev-secret               # Windows (cmd.exe)
```
