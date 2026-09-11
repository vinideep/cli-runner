"""
cli-runner — High-throughput execution gateway for subscription-based AI coding agents.

Features:
- Synchronous and Streaming (SSE) agent execution.
- Background asynchronous job queue with worker pool & cancellation.
- Dynamic Agent Router for task/cost/speed-based agent selection.
- Multi-process concurrency control with isolated workspaces.
- Prometheus metrics exposition (/metrics).
- Cross-platform daemon support (Linux systemd, macOS launchd, Windows Task Scheduler).
- Hardened token authentication & IP allowlisting.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import itertools
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sys
import tempfile
import time
from typing import Any, AsyncGenerator, Dict, List, Optional
import uuid

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

# ── Config (env) ─────────────────────────────────────────────────────────────
PLATFORM_SYSTEM = platform.system().lower()  # "linux" | "darwin" | "windows"
IS_WINDOWS = PLATFORM_SYSTEM == "windows"

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
CLI_ALLOWED_IPS = os.environ.get("CLI_ALLOWED_IPS", "").strip()
WORKSPACE_DIR = os.environ.get("CLI_WORKSPACE_DIR", _DEFAULT_WORKSPACE)
CMD_BIN = os.environ.get("CMD_BIN", _DEFAULT_CMD_BIN)
AGY_BIN = os.environ.get("AGY_BIN", DEFAULT_AGY_BIN)
DEFAULT_TIMEOUT = int(os.environ.get("CLI_DEFAULT_TIMEOUT", "1800"))
CMD_MAX_TURNS = os.environ.get("CMD_MAX_TURNS", "10")
BIND_HOST = os.environ.get("CLI_BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLI_PORT", "8899"))
MAX_CONCURRENCY = max(1, int(os.environ.get("CLI_MAX_CONCURRENCY", "5")))
MAX_OUTPUT_CHARS = int(os.environ.get("CLI_MAX_OUTPUT_CHARS", "1000000"))

_sem = asyncio.Semaphore(MAX_CONCURRENCY)

# Live queue counters
_req_counter = itertools.count(1)
_in_flight = 0
_waiting = 0

# Observability / Prometheus metrics storage
_metrics: dict[str, Any] = {
    "requests_total": {},         # (provider, status) -> count
    "requests_failed_total": {},  # (provider, reason) -> count
    "duration_seconds_sum": {},   # provider -> float
    "duration_seconds_count": {}, # provider -> int
    "retries_total": 0,
}

# Active execution tracking & async jobs
_active_processes: dict[str, asyncio.subprocess.Process] = {}
_jobs: dict[str, dict] = {}
_job_queue: asyncio.Queue[str] = asyncio.Queue()


def _log(msg: str) -> None:
    print(f"[cli-runner] {msg}", flush=True)


def _record_metric(provider: str, status: str, duration_s: float, error_reason: Optional[str] = None) -> None:
    key = (provider, status)
    _metrics["requests_total"][key] = _metrics["requests_total"].get(key, 0) + 1
    if status != "success":
        r_key = (provider, error_reason or "general_error")
        _metrics["requests_failed_total"][r_key] = _metrics["requests_failed_total"].get(r_key, 0) + 1
    _metrics["duration_seconds_sum"][provider] = _metrics["duration_seconds_sum"].get(provider, 0.0) + duration_s
    _metrics["duration_seconds_count"][provider] = _metrics["duration_seconds_count"].get(provider, 0) + 1


def _check_ip_allowed(client_host: Optional[str]) -> None:
    if not CLI_ALLOWED_IPS or not client_host:
        return
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid client IP address.")

    for entry in CLI_ALLOWED_IPS.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return
            elif ip == ipaddress.ip_address(entry):
                return
        except ValueError:
            continue
    raise HTTPException(status_code=403, detail="Client IP not permitted by allowlist.")


def _verify_token(x_cli_token: str, request: Request) -> None:
    if CLI_ALLOWED_IPS and request.client:
        _check_ip_allowed(request.client.host)
    if not CLI_TOKEN:
        raise HTTPException(status_code=503, detail="Service has no CLI_TOKEN configured.")
    if not secrets.compare_digest(x_cli_token or "", CLI_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing X-CLI-Token.")


# ── Agent Router ─────────────────────────────────────────────────────────────
class RouteRequest(BaseModel):
    task: Optional[str] = Field(None, description="Task category e.g. refactor, quick_fix, audit")
    prompt: Optional[str] = Field(None, description="Prompt text to evaluate")
    requirements: Optional[Dict[str, str]] = Field(None, description="e.g. {'speed': 'high', 'reasoning': 'high', 'cost': 'low'}")


def route_agent(
    task: Optional[str] = None,
    prompt: Optional[str] = None,
    requirements: Optional[Dict[str, str]] = None,
) -> dict:
    reqs = {k.lower(): str(v).lower() for k, v in (requirements or {}).items()}
    task_clean = (task or "").strip().lower()
    speed = reqs.get("speed")
    reasoning = reqs.get("reasoning")
    cost = reqs.get("cost")

    agy_available = shutil.which(AGY_BIN) is not None or os.path.exists(AGY_BIN)
    cmd_available = shutil.which(CMD_BIN) is not None

    if speed == "high" or cost == "low" or any(k in task_clean for k in ("quick", "lint", "format", "fast", "snippet")):
        provider = "agy" if agy_available else "cmd"
        effort = "low"
        reason = "Optimized for low-latency / high-throughput batch task"
    elif reasoning == "high" or any(k in task_clean for k in ("refactor", "architect", "deep", "complex", "audit", "security")):
        provider = "cmd" if cmd_available else "agy"
        effort = "high"
        reason = "Selected for deep multi-file reasoning and complex context"
    else:
        provider = "cmd" if cmd_available else "agy"
        effort = "medium"
        reason = "Balanced default execution profile"

    return {
        "provider": provider,
        "effort": effort,
        "model": None,
        "reason": reason,
    }


# ── Schemas ──────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    provider: str = Field(..., description="'cmdcode', 'agy', or 'auto'")
    prompt: str = Field(..., min_length=1)
    model: Optional[str] = None
    effort: Optional[str] = None
    timeout: Optional[int] = None
    task: Optional[str] = None
    requirements: Optional[Dict[str, str]] = None


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Background Worker Loop ───────────────────────────────────────────────────
async def _job_worker_loop() -> None:
    while True:
        try:
            job_id = await _job_queue.get()
            job = _jobs.get(job_id)
            if not job or job["status"] == JobStatus.CANCELLED:
                _job_queue.task_done()
                continue

            job["status"] = JobStatus.RUNNING
            job["started_at"] = time.time()

            result = await _execute_cli(
                job_id=job_id,
                provider=job["provider"],
                prompt=job["prompt"],
                model=job["model"],
                effort=job["effort"],
                timeout=job["timeout"],
                task=job.get("task"),
                requirements=job.get("requirements"),
            )

            if job["status"] != JobStatus.CANCELLED:
                job["status"] = JobStatus.COMPLETED if result.get("ok") else JobStatus.FAILED
                job["response"] = result.get("response")
                job["error"] = result.get("error")
                job["exit_code"] = result.get("exit_code")
                job["latency_ms"] = result.get("latency_ms")
                job["completed_at"] = time.time()

            _job_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _log(f"Unhandled error in job worker loop: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _print_startup_banner()
    worker_task = asyncio.create_task(_job_worker_loop())
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="cli-runner", version="2.0.0", lifespan=lifespan)


def _resolve_bin(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("cmdcode", "cmdcode-cli", "cmd", "command-code"):
        return "cmd"
    if p in ("agy", "agy-cli", "antigravity"):
        return "agy"
    if p in ("auto", "router", "gateway"):
        return "auto"
    raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'.")


def _validate_effort(effort: Optional[str]) -> Optional[str]:
    if effort is None:
        return None
    e = effort.strip().lower()
    if e not in ("low", "medium", "high"):
        raise HTTPException(
            status_code=400, detail=f"Invalid effort '{effort}'. Must be low, medium, or high.")
    return e


def _build_argv(kind: str, prompt: str, model: Optional[str],
                effort: Optional[str]) -> list[str]:
    if kind == "cmd":
        argv = [CMD_BIN, "-p", prompt, "--yolo", "--skip-onboarding",
                "--max-turns", str(CMD_MAX_TURNS)]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        return argv
    argv = [AGY_BIN, "--prompt", prompt, "--dangerously-skip-permissions"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv


def _platform_windows_resolve(argv: list[str]) -> list[str]:
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
    try:
        resolved = shutil.which(bin_path)
    except TypeError:
        resolved = None
    if not resolved and not (kind == "agy" and os.path.exists(bin_path)):
        return f"not found (bin '{bin_path}')"
    base = Path(os.environ.get("USERPROFILE", Path.home())) if IS_WINDOWS else Path(os.environ.get("HOME", Path.home()))
    cred_dir = base / ".commandcode"
    auth_file = cred_dir / "auth.json"
    try:
        if not auth_file.exists():
            return f"installed, NOT logged in (no {auth_file})"
    except OSError:
        return "installed (credentials check restricted)"
    return "installed + authenticated"


def _print_startup_banner() -> None:
    cmd_status = _auth_status("cmd", CMD_BIN)
    agy_status = _auth_status("agy", AGY_BIN)
    _log("┌─ startup")
    _log(f"│  platform:    {platform.platform()}")
    _log(f"│  cmd:         {CMD_BIN} ({cmd_status})")
    _log(f"│  agy:         {AGY_BIN} ({agy_status})")
    _log(f"│  workspace:   {WORKSPACE_DIR}")
    _log(f"│  bind:        {BIND_HOST}:{PORT}")
    _log(f"│  concurrency: {MAX_CONCURRENCY}")
    if CLI_ALLOWED_IPS:
        _log(f"│  ip_allow:    {CLI_ALLOWED_IPS}")
    _log("└─ listening")


# ── Execution Core ───────────────────────────────────────────────────────────
async def _execute_cli(
    job_id: str,
    provider: str,
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout: Optional[int] = None,
    task: Optional[str] = None,
    requirements: Optional[Dict[str, str]] = None,
) -> dict:
    global _in_flight, _waiting

    kind = _resolve_bin(provider)
    if kind == "auto":
        routed = route_agent(task=task, prompt=prompt, requirements=requirements)
        kind = routed["provider"]
        if effort is None:
            effort = routed["effort"]

    effort = _validate_effort(effort)
    t_out = float(timeout or DEFAULT_TIMEOUT)
    argv = _build_argv(kind, prompt, (model or "").strip() or None, effort)

    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    run_dir = tempfile.mkdtemp(prefix="run-", dir=WORKSPACE_DIR)

    started = time.monotonic()
    _waiting += 1
    try:
        await _sem.acquire()
    finally:
        _waiting -= 1

    _in_flight += 1
    queued_ms = int((time.monotonic() - started) * 1000)
    _log(f"[{job_id}] started after {queued_ms}ms queue wait (in_flight={_in_flight}, waiting={_waiting})")

    try:
        if kind == "agy":
            argv += ["--print-timeout", f"{int(t_out)}s"]

        sub_env = os.environ.copy()
        if kind == "cmd" and not IS_WINDOWS:
            real_home = os.environ.get("HOME", os.path.expanduser("~"))
            real_cmd_dir = os.path.join(real_home, ".commandcode")
            isolated_home = os.path.join(run_dir, "home")
            isolated_cmd_dir = os.path.join(isolated_home, ".commandcode")
            os.makedirs(isolated_cmd_dir, exist_ok=True)
            for fname in ("auth.json", "config.json"):
                src_file = os.path.join(real_cmd_dir, fname)
                try:
                    if os.path.exists(src_file):
                        shutil.copy2(src_file, isolated_cmd_dir)
                except OSError:
                    pass
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
            _active_processes[job_id] = proc
        except FileNotFoundError as e:
            _log(f"[{job_id}] FAILED to spawn: {e}")
            _record_metric(kind, "failed", time.monotonic() - started, "binary_not_found")
            raise HTTPException(status_code=500, detail=f"CLI binary not found: {e}")

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=t_out)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            latency_ms = int((time.monotonic() - started) * 1000)
            _record_metric(kind, "timeout", latency_ms / 1000.0, "timeout")
            _log(f"[{job_id}] TIMED OUT after {t_out:.0f}s (latency_ms={latency_ms})")
            return {
                "ok": False,
                "response": None,
                "error": f"CLI timed out after {t_out:.0f}s",
                "latency_ms": latency_ms,
                "exit_code": -1,
            }
    finally:
        _in_flight -= 1
        _active_processes.pop(job_id, None)
        _sem.release()
        shutil.rmtree(run_dir, ignore_errors=True)

    latency_ms = int((time.monotonic() - started) * 1000)
    output = (stdout or b"").decode("utf-8", errors="replace").strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... [TRUNCATED DUE TO MAX OUTPUT LIMIT]"

    rc = proc.returncode

    if rc != 0 or not output:
        _record_metric(kind, "failed", latency_ms / 1000.0, f"exit_{rc}")
        _log(f"[{job_id}] finished ok=False rc={rc} latency_ms={latency_ms}. Output: {output[:300]}")
        err_msg = output if len(output) <= 1000 else (output[:500] + "\n... [TRUNCATED] ...\n" + output[-500:])
        return {
            "ok": False,
            "response": output or None,
            "error": f"CLI exit={rc}: {err_msg}",
            "latency_ms": latency_ms,
            "exit_code": rc,
        }

    _record_metric(kind, "success", latency_ms / 1000.0)
    _log(f"[{job_id}] finished ok=True rc=0 latency_ms={latency_ms} chars={len(output)}")
    return {
        "ok": True,
        "response": output,
        "error": None,
        "latency_ms": latency_ms,
        "exit_code": rc,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        # Safe readiness fact: never expose the token, only whether /run can
        # authenticate callers. This lets the backend distinguish a reachable
        # but unusable runner from a fully configured one.
        "auth_configured": bool(CLI_TOKEN),
        "cmd_found": shutil.which(CMD_BIN) is not None,
        "agy_found": shutil.which(AGY_BIN) is not None or os.path.exists(AGY_BIN),
        "workspace": WORKSPACE_DIR,
        "max_concurrency": MAX_CONCURRENCY,
        "platform": PLATFORM_SYSTEM,
        "in_flight": _in_flight,
        "waiting": _waiting,
        "active_jobs": len(_active_processes),
        "queued_jobs": _job_queue.qsize(),
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    lines = [
        "# HELP cli_runner_requests_total Total number of agent execution requests.",
        "# TYPE cli_runner_requests_total counter",
    ]
    if _metrics["requests_total"]:
        for (prov, st), cnt in _metrics["requests_total"].items():
            lines.append(f'cli_runner_requests_total{{provider="{prov}",status="{st}"}} {cnt}')
    else:
        lines.append('cli_runner_requests_total{provider="default",status="idle"} 0')

    lines.extend([
        "# HELP cli_runner_requests_failed_total Total number of failed requests.",
        "# TYPE cli_runner_requests_failed_total counter",
    ])
    for (prov, rsn), cnt in _metrics["requests_failed_total"].items():
        lines.append(f'cli_runner_requests_failed_total{{provider="{prov}",reason="{rsn}"}} {cnt}')

    lines.extend([
        "# HELP cli_runner_active_processes Current number of active CLI processes.",
        "# TYPE cli_runner_active_processes gauge",
        f"cli_runner_active_processes {_in_flight}",
        "# HELP cli_runner_waiting_requests Current number of requests queued behind semaphore.",
        "# TYPE cli_runner_waiting_requests gauge",
        f"cli_runner_waiting_requests {_waiting}",
        "# HELP cli_runner_queued_jobs Current number of async jobs waiting in queue.",
        "# TYPE cli_runner_queued_jobs gauge",
        f"cli_runner_queued_jobs {_job_queue.qsize()}",
        "# HELP cli_runner_duration_seconds_sum Total execution duration in seconds.",
        "# TYPE cli_runner_duration_seconds_sum counter",
    ])
    for prov, dur in _metrics["duration_seconds_sum"].items():
        lines.append(f'cli_runner_duration_seconds_sum{{provider="{prov}"}} {dur:.4f}')

    lines.extend([
        "# HELP cli_runner_duration_seconds_count Total execution requests timed.",
        "# TYPE cli_runner_duration_seconds_count counter",
    ])
    for prov, cnt in _metrics["duration_seconds_count"].items():
        lines.append(f'cli_runner_duration_seconds_count{{provider="{prov}"}} {cnt}')

    return "\n".join(lines) + "\n"


@app.post("/route")
async def route_task_endpoint(
    req: RouteRequest,
    request: Request,
    x_cli_token: str = Header(default=""),
) -> dict:
    _verify_token(x_cli_token, request)
    return route_agent(req.task, req.prompt, req.requirements)


@app.post("/run")
async def run(
    req: RunRequest,
    request: Request,
    x_cli_token: str = Header(default=""),
) -> dict:
    _verify_token(x_cli_token, request)
    req_id = f"run-{next(_req_counter)}"
    result = await _execute_cli(
        job_id=req_id,
        provider=req.provider,
        prompt=req.prompt,
        model=req.model,
        effort=req.effort,
        timeout=req.timeout,
        task=req.task,
        requirements=req.requirements,
    )
    return {
        "ok": result["ok"],
        "response": result["response"],
        "error": result["error"],
        "latency_ms": result["latency_ms"],
    }


@app.post("/run/stream")
async def run_stream(
    req: RunRequest,
    request: Request,
    x_cli_token: str = Header(default=""),
):
    _verify_token(x_cli_token, request)
    global _in_flight, _waiting

    kind = _resolve_bin(req.provider)
    if kind == "auto":
        routed = route_agent(task=req.task, prompt=req.prompt, requirements=req.requirements)
        kind = routed["provider"]
        if req.effort is None:
            req.effort = routed["effort"]

    effort = _validate_effort(req.effort)
    t_out = float(req.timeout or DEFAULT_TIMEOUT)
    argv = _build_argv(kind, req.prompt, (req.model or "").strip() or None, effort)
    stream_id = f"stream-{next(_req_counter)}"

    async def event_generator() -> AsyncGenerator[str, None]:
        global _in_flight, _waiting
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        run_dir = tempfile.mkdtemp(prefix="run-", dir=WORKSPACE_DIR)
        started = time.monotonic()

        _waiting += 1
        yield f"event: queued\ndata: {json.dumps({'stream_id': stream_id, 'provider': kind})}\n\n"
        try:
            await _sem.acquire()
        finally:
            _waiting -= 1

        _in_flight += 1
        yield f"event: start\ndata: {json.dumps({'stream_id': stream_id, 'provider': kind, 'effort': effort})}\n\n"

        try:
            if kind == "agy":
                argv.extend(["--print-timeout", f"{int(t_out)}s"])

            sub_env = os.environ.copy()
            if kind == "cmd" and not IS_WINDOWS:
                real_home = os.environ.get("HOME", os.path.expanduser("~"))
                real_cmd_dir = os.path.join(real_home, ".commandcode")
                isolated_home = os.path.join(run_dir, "home")
                isolated_cmd_dir = os.path.join(isolated_home, ".commandcode")
                os.makedirs(isolated_cmd_dir, exist_ok=True)
                for fname in ("auth.json", "config.json"):
                    src_file = os.path.join(real_cmd_dir, fname)
                    try:
                        if os.path.exists(src_file):
                            shutil.copy2(src_file, isolated_cmd_dir)
                    except OSError:
                        pass
                sub_env["HOME"] = isolated_home

            argv_to_run = _platform_windows_resolve(argv) if IS_WINDOWS else argv
            proc = await asyncio.create_subprocess_exec(
                *argv_to_run,
                cwd=run_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sub_env,
            )
            _active_processes[stream_id] = proc

            total_chars = 0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                total_chars += len(text)
                if total_chars > MAX_OUTPUT_CHARS:
                    yield f"event: chunk\ndata: {json.dumps({'chunk': '... [TRUNCATED]'})}\n\n"
                    proc.kill()
                    break
                yield f"event: chunk\ndata: {json.dumps({'chunk': text})}\n\n"

            rc = await proc.wait()
            dur_ms = int((time.monotonic() - started) * 1000)
            _record_metric(kind, "success" if rc == 0 else "failed", dur_ms / 1000.0)
            yield f"event: done\ndata: {json.dumps({'ok': rc == 0, 'exit_code': rc, 'latency_ms': dur_ms})}\n\n"
        except Exception as exc:
            dur_ms = int((time.monotonic() - started) * 1000)
            _record_metric(kind, "failed", dur_ms / 1000.0, "stream_exception")
            yield f"event: error\ndata: {json.dumps({'error': str(exc), 'latency_ms': dur_ms})}\n\n"
        finally:
            _in_flight -= 1
            _active_processes.pop(stream_id, None)
            _sem.release()
            shutil.rmtree(run_dir, ignore_errors=True)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/jobs", status_code=202)
async def create_job(
    req: RunRequest,
    request: Request,
    x_cli_token: str = Header(default=""),
) -> dict:
    _verify_token(x_cli_token, request)
    _resolve_bin(req.provider)
    _validate_effort(req.effort)

    job_id = f"job-{uuid.uuid4().hex[:12]}"
    job_record = {
        "id": job_id,
        "status": JobStatus.QUEUED,
        "provider": req.provider,
        "prompt": req.prompt,
        "model": req.model,
        "effort": req.effort,
        "timeout": req.timeout or DEFAULT_TIMEOUT,
        "task": req.task,
        "requirements": req.requirements,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "duration_ms": None,
        "response": None,
        "error": None,
        "exit_code": None,
    }
    _jobs[job_id] = job_record
    await _job_queue.put(job_id)
    return {"ok": True, "job_id": job_id, "status": JobStatus.QUEUED}


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    request: Request,
    x_cli_token: str = Header(default=""),
) -> dict:
    _verify_token(x_cli_token, request)
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return _jobs[job_id]


@app.get("/jobs")
async def list_jobs(
    request: Request,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, le=200),
    x_cli_token: str = Header(default=""),
) -> dict:
    _verify_token(x_cli_token, request)
    results = list(_jobs.values())
    if status:
        results = [j for j in results if j.get("status") == status]
    return {"jobs": results[-limit:]}


@app.delete("/jobs/{job_id}")
async def cancel_job(
    job_id: str,
    request: Request,
    x_cli_token: str = Header(default=""),
) -> dict:
    _verify_token(x_cli_token, request)
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    job = _jobs[job_id]
    if job["status"] in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        return {"ok": False, "detail": f"Job already in terminal state '{job['status']}'."}

    job["status"] = JobStatus.CANCELLED
    proc = _active_processes.get(job_id)
    if proc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    return {"ok": True, "job_id": job_id, "status": JobStatus.CANCELLED}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND_HOST, port=PORT, log_level="warning")
