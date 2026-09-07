"""
cli-runner — a tiny host-side HTTP bridge that runs the cmdcode / agy CLIs.

Why this exists
---------------
The Command Code (`cmd`) and Antigravity (`agy`) CLIs are authenticated against
the HOST (their credentials live under the host user's home, e.g. /root), so they
can NOT run inside the Dockerized backend container. Previously the backend
reached them through an elaborate async job queue drained by host cron scripts.
That queue was the source of stuck jobs, prose-instead-of-JSON (from
conversation reuse), and silent whole-batch drops.

This service replaces all of that: it runs directly on the host (systemd, NOT
Docker), exposes ONE token-gated endpoint, and the backend calls it synchronously
per prompt. No queue, no cron, no polling.

Design notes
------------
- Each call runs a FRESH CLI process (no -c/--continue). Conversation reuse is
  exactly what made `cmd` reply with a prose summary instead of the JSON array.
- The CLI is invoked from an ISOLATED workspace dir, never the app directory —
  `cmd` is a coding agent that will happily explore whatever cwd it's launched in.
- A module-level semaphore serialises CLI runs so the backend's concurrent prompt
  dispatch can't spawn N heavy CLI processes at once (or violate a single-session
  CLI subscription).
- Bound per CLI_BIND_HOST (default 0.0.0.0) and token-gated. The CLI runs as
  the host user (root), so the token file must be kept private (chmod 600 on
  the env file).
- The prompt is passed as a single argv element via create_subprocess_exec — no
  shell, no interpolation, no injection surface.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# ── Config (env) ─────────────────────────────────────────────────────────────
# Every value below can still be overridden via the environment (the Linux
# systemd unit does exactly that). The defaults are just OS-appropriate
# starting points so `python main.py` works out of the box on any OS.
PLATFORM_SYSTEM = platform.system().lower()  # "linux" | "darwin" | "windows"
IS_WINDOWS = PLATFORM_SYSTEM == "windows"

# The `cmd` npm package installs different launchers per OS. On Windows the
# shell shim is `cmd.cmd` (plain `cmd` would resolve to the built-in cmd.exe
# shell!) and Node does not honour the HOME env var — the CLI puts its
# credentials in %USERPROFILE%\.commandcode instead, so no HOME isolation is
# needed there. On Linux/macOS the CLI is `cmd` and reads ~/.commandcode,
# which is why isolated-HOME copying exists for those platforms.
_WINDOWS_CMD_SHIMS = ["cmd.cmd", "cmd.exe", "cmd.ps1"]
_DEFAULT_CMD_BIN = "cmd.cmd" if IS_WINDOWS else "cmd"

DEFAULT_AGY_BIN = {
    "windows": "agy.cmd",
    "darwin": shutil.which("agy") or os.path.expanduser("~/.local/bin/agy"),
    "linux": "/root/.local/bin/agy",
}[PLATFORM_SYSTEM]

_WINDOWS_WS = str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "cli-runner-workspace")
_DEFAULT_WORKSPACE = _WINDOWS_WS if IS_WINDOWS else "/tmp/cli-runner-workspace"

CLI_TOKEN = os.environ.get("CLI_TOKEN", "").strip()
WORKSPACE_DIR = os.environ.get("CLI_WORKSPACE_DIR", _DEFAULT_WORKSPACE)
CMD_BIN = os.environ.get("CMD_BIN", _DEFAULT_CMD_BIN)
AGY_BIN = os.environ.get("AGY_BIN", DEFAULT_AGY_BIN)
DEFAULT_TIMEOUT = int(os.environ.get("CLI_DEFAULT_TIMEOUT", "1800"))
CMD_MAX_TURNS = os.environ.get("CMD_MAX_TURNS", "10")
BIND_HOST = os.environ.get("CLI_BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLI_PORT", "8899"))
# How many CLI processes run at once. A full batch pass (CHG-146/149) dispatches
# dozens of prompts; serialising them one-at-a-time (the old default of 1) is
# the single biggest cause of a full run taking hours. Each concurrent run gets
# its OWN subdirectory under WORKSPACE_DIR (see _run.workspace below), so
# raising this is safe from a filesystem-collision standpoint — the remaining
# constraint is whatever concurrent-session limit your CLI subscription/plan
# allows. Start at 5 on the production host and raise/lower based on observed
# CPU, memory, provider rate-limit, and failure metrics.
MAX_CONCURRENCY = max(1, int(os.environ.get("CLI_MAX_CONCURRENCY", "5")))

_sem = asyncio.Semaphore(MAX_CONCURRENCY)

# Request-visibility gauges (CHG-151). Uvicorn's access log only prints a
# request line when the response COMPLETES — a run parked behind the semaphore
# for an hour is invisible there, which looked exactly like "the backend isn't
# calling the API" in production while 80+ batch prompts sat queued. These
# counters + the _log lines below make every request visible the moment it
# lands, and /health exposes live queue depth.
_req_counter = itertools.count(1)
_in_flight = 0   # CLI processes currently running
_waiting = 0     # requests parked in the semaphore queue


def _log(msg: str) -> None:
    # stdout → journald (systemd captures it alongside uvicorn's own lines).
    print(f"[cli-runner] {msg}", flush=True)


app = FastAPI(title="cli-runner", version="1.0.0")


class RunRequest(BaseModel):
    provider: str = Field(..., description="'cmdcode' or 'agy'")
    prompt: str = Field(..., min_length=1)
    model: Optional[str] = None
    effort: Optional[str] = None
    timeout: Optional[int] = None


def _resolve_bin(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("cmdcode", "cmdcode-cli", "cmd", "command-code"):
        return "cmd"
    if p in ("agy", "agy-cli", "antigravity"):
        return "agy"
    raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'.")


def _validate_effort(effort: Optional[str]) -> Optional[str]:
    """Both CLIs accept --effort low|medium|high. Reject anything else up front
    instead of spawning a CLI that will just fail flag parsing."""
    if effort is None:
        return None
    e = effort.strip().lower()
    if e not in ("low", "medium", "high"):
        raise HTTPException(
            status_code=400, detail=f"Invalid effort '{effort}'. Must be low, medium, or high.")
    return e


def _build_argv(kind: str, prompt: str, model: Optional[str],
                effort: Optional[str]) -> list[str]:
    """Mirror the exact CLI invocations the old host workers used, minus the
    conversation-continuation flag (always a fresh session)."""
    if kind == "cmd":
        # cmd -p PROMPT --yolo --skip-onboarding --max-turns N
        argv = [CMD_BIN, "-p", prompt, "--yolo", "--skip-onboarding",
                "--max-turns", str(CMD_MAX_TURNS)]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        return argv
    # agy --prompt PROMPT --dangerously-skip-permissions --print-timeout N
    argv = [AGY_BIN, "--prompt", prompt, "--dangerously-skip-permissions"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv


def _platform_windows_resolve(argv: list[str]) -> list[str]:
    """Windows npm installs the `cmd` CLI as `cmd.cmd` (a batch file), which
    CreateProcess cannot execute directly — it has to be run through the
    system shell. Node shims also shadow each other based on PATH order, so we
    resolve the executable ourselves and pass it to cmd.exe /c (arguments are
    passed verbatim, so there is still no shell interpolation of the prompt)."""
    exe = argv[0]
    if exe.endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", *argv]
    resolved = shutil.which(exe)
    if resolved and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved, *argv[1:]]
    if exe == "cmd" or exe in _WINDOWS_CMD_SHIMS:
        for candidate in _WINDOWS_CMD_SHIMS:
            found = shutil.which(candidate)
            if found:
                return ["cmd.exe", "/c", found, *argv[1:]]
    return argv


def _auth_status(kind: str, bin_path: str) -> str:
    """Probe whether a CLI is installed and authenticated so the startup
    banner can tell the operator what (if anything) is missing."""
    try:
        resolved = shutil.which(bin_path)
    except TypeError:
        resolved = None
    if not resolved and not (kind == "agy" and os.path.exists(bin_path)):
        return f"not found (bin '{bin_path}')"
    # Look for the credentials the CLI would use — this matches what the CLI
    # itself consults: ~/.commandcode on POSIX, %USERPROFILE%\.commandcode on
    # Windows (Node ignores the HOME env var there).
    base = Path(os.environ.get("USERPROFILE", Path.home())) if IS_WINDOWS else Path.home()
    cred_dir = base / ".commandcode"
    auth_file = cred_dir / "auth.json"
    if not auth_file.exists():
        return f"installed, NOT logged in (no {auth_file})"
    return "installed + authenticated"


def _print_startup_banner() -> None:
    """Summarise the auto-detected OS configuration once at startup."""
    cmd_status = _auth_status("cmd", CMD_BIN)
    agy_status = _auth_status("agy", AGY_BIN)
    _log("┌─ startup")
    _log(f"│  platform:  {platform.platform()}")
    _log(f"│  cmd:       {CMD_BIN} ({cmd_status})")
    _log(f"│  agy:       {AGY_BIN} ({agy_status})")
    _log(f"│  workspace: {WORKSPACE_DIR}")
    _log(f"│  bind:      {BIND_HOST}:{PORT}")
    _log(f"│  concurrency: {MAX_CONCURRENCY}")
    _log("└─ listening")


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "cmd_found": shutil.which(CMD_BIN) is not None,
        "agy_found": shutil.which(AGY_BIN) is not None or os.path.exists(AGY_BIN),
        "workspace": WORKSPACE_DIR,
        "max_concurrency": MAX_CONCURRENCY,
        "platform": PLATFORM_SYSTEM,
        # Live queue depth — lets the backend (and a curious operator) see that
        # requests ARE arriving even when none have completed yet.
        "in_flight": _in_flight,
        "waiting": _waiting,
    }


@app.post("/run")
async def run(req: RunRequest, x_cli_token: str = Header(default="")) -> dict:
    global _in_flight, _waiting

    if not CLI_TOKEN:
        raise HTTPException(status_code=503, detail="Service has no CLI_TOKEN configured.")
    if x_cli_token != CLI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-CLI-Token.")

    kind = _resolve_bin(req.provider)
    effort = _validate_effort(req.effort)
    timeout = float(req.timeout or DEFAULT_TIMEOUT)
    argv = _build_argv(kind, req.prompt, (req.model or "").strip() or None, effort)

    req_id = next(_req_counter)
    _log(
        f"run#{req_id} received: provider={kind} prompt_chars={len(req.prompt)} "
        f"effort={effort or 'default'} timeout={timeout:.0f}s "
        f"(in_flight={_in_flight}, waiting={_waiting})"
    )

    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    # Each concurrent run gets its OWN subdirectory — with MAX_CONCURRENCY > 1,
    # multiple CLI processes run at the same time, and `cmd` in particular is a
    # coding agent that reads/writes scratch files in its cwd. Sharing one
    # directory across concurrent runs would let them collide/interfere.
    run_dir = tempfile.mkdtemp(prefix="run-", dir=WORKSPACE_DIR)

    started = time.monotonic()
    _waiting += 1
    try:
        await _sem.acquire()
    finally:
        _waiting -= 1

    _in_flight += 1
    queued_ms = int((time.monotonic() - started) * 1000)
    _log(
        f"run#{req_id} started after {queued_ms}ms in queue "
        f"(in_flight={_in_flight}, waiting={_waiting})"
    )
    try:
        # agy has a native --print-timeout; cmd does not, so we bound it here.
        if kind == "agy":
            argv += ["--print-timeout", f"{int(timeout)}s"]
        # Prepare environment with isolated HOME for cmdcode
        sub_env = os.environ.copy()
        if kind == "cmd" and not IS_WINDOWS:
            real_home = os.path.expanduser("~")
            real_cmd_dir = os.path.join(real_home, ".commandcode")
            isolated_home = os.path.join(run_dir, "home")
            isolated_cmd_dir = os.path.join(isolated_home, ".commandcode")
            os.makedirs(isolated_cmd_dir, exist_ok=True)
            for fname in ("auth.json", "config.json"):
                src_file = os.path.join(real_cmd_dir, fname)
                if os.path.exists(src_file):
                    shutil.copy2(src_file, isolated_cmd_dir)
            sub_env["HOME"] = isolated_home

        argv_to_run = _platform_windows_resolve(argv) if IS_WINDOWS else argv
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv_to_run,
                cwd=run_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sub_env,
            )
        except FileNotFoundError as e:
            _log(f"run#{req_id} FAILED to spawn: {e}")
            raise HTTPException(status_code=500, detail=f"CLI binary not found: {e}")

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            latency_ms = int((time.monotonic() - started) * 1000)
            _log(f"run#{req_id} TIMED OUT after {timeout:.0f}s (latency_ms={latency_ms})")
            return {"ok": False, "response": None,
                    "error": f"CLI timed out after {timeout:.0f}s", "latency_ms": latency_ms}
    finally:
        _in_flight -= 1
        _sem.release()
        shutil.rmtree(run_dir, ignore_errors=True)

    latency_ms = int((time.monotonic() - started) * 1000)
    output = (stdout or b"").decode("utf-8", errors="replace").strip()
    rc = proc.returncode

    if rc != 0 or not output:
        # Return both first/last segments so the backend can see the actual error at the beginning
        # (e.g. flag parsing error) as well as the help text/usage tail.
        _log(f"run#{req_id} finished ok=False rc={rc} latency_ms={latency_ms}. Full output:\n{output}")
        err_msg = output if len(output) <= 1000 else (output[:500] + "\n... [TRUNCATED] ...\n" + output[-500:])
        return {"ok": False, "response": output or None,
                "error": f"CLI exit={rc}: {err_msg}", "latency_ms": latency_ms}

    _log(
        f"run#{req_id} finished ok=True rc=0 latency_ms={latency_ms} "
        f"output_chars={len(output)}"
    )
    return {"ok": True, "response": output, "error": None, "latency_ms": latency_ms}


if __name__ == "__main__":
    # `python main.py` just works on any OS: uvicorn is imported here, the OS
    # has already been auto-detected at import time, and the banner shows what
    # was detected. On Linux in production, the systemd unit is still the
    # right way to run this — but plain `python main.py` is enough on
    # macOS/Windows (and for dev).
    import uvicorn

    _print_startup_banner()
    uvicorn.run(app, host=BIND_HOST, port=PORT, log_level="warning")
