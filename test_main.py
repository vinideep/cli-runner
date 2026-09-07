"""Tests for the cli-runner service. Uses stub CLI scripts so no real CLI or
auth is needed."""

import asyncio
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

import main  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # A stub binary that ignores its args and prints a known JSON array
    stub = tmp_path / "stub-cmd"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        echo '[{"ok":true}]'
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    fake_home = tmp_path / "fake-user-home"
    fake_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLI_TOKEN", "secret")
    monkeypatch.setenv("CMD_BIN", str(stub))
    monkeypatch.setenv("AGY_BIN", str(stub))
    monkeypatch.setenv("CLI_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("CLI_ALLOWED_IPS", "")

    for mod in ("main",):
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(Path(__file__).parent))
    import main  # noqa: E402
    return TestClient(main.app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["in_flight"] == 0
    assert body["waiting"] == 0
    assert body["max_concurrency"] >= 1
    assert "active_jobs" in body
    assert "queued_jobs" in body


def test_run_returns_cli_stdout(client):
    r = client.post(
        "/run",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "cmdcode", "prompt": "analyze"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["response"] == '[{"ok":true}]'
    assert body["error"] is None


def test_missing_token_rejected(client):
    r = client.post("/run", json={"provider": "cmdcode", "prompt": "x"})
    assert r.status_code == 401


def test_bad_token_rejected(client):
    r = client.post(
        "/run",
        headers={"X-CLI-Token": "wrong"},
        json={"provider": "cmdcode", "prompt": "x"},
    )
    assert r.status_code == 401


def test_unknown_provider_rejected(client):
    r = client.post(
        "/run",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "gpt5", "prompt": "x"},
    )
    assert r.status_code == 400


def test_default_concurrency_is_raised_from_the_old_serialised_default(client):
    body = client.get("/health").json()
    assert body["max_concurrency"] >= 3


def test_argv_never_includes_continuation_flag():
    sys.modules.pop("main", None)
    import main
    for kind in ("cmd", "agy"):
        argv = main._build_argv(kind, "some prompt", model=None, effort=None)
        assert "-c" not in argv
        assert "--continue" not in argv


def test_effort_flag_added_when_requested():
    sys.modules.pop("main", None)
    import main
    for kind in ("cmd", "agy"):
        argv = main._build_argv(kind, "prompt", model=None, effort="low")
        assert "--effort" in argv
        assert argv[argv.index("--effort") + 1] == "low"


def test_effort_not_added_by_default():
    sys.modules.pop("main", None)
    import main
    for kind in ("cmd", "agy"):
        argv = main._build_argv(kind, "prompt", model=None, effort=None)
        assert "--effort" not in argv


def test_invalid_effort_rejected(client):
    r = client.post(
        "/run",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "cmdcode", "prompt": "x", "effort": "turbo"},
    )
    assert r.status_code == 400


def test_concurrent_runs_use_isolated_workspace_dirs(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    stub = tmp_path / "stub-pwd"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        pwd
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLI_TOKEN", "secret")
    monkeypatch.setenv("CMD_BIN", str(stub))
    monkeypatch.setenv("CLI_WORKSPACE_DIR", str(workspace))
    sys.modules.pop("main", None)
    import main
    c = TestClient(main.app)

    dirs = set()
    for _ in range(2):
        r = c.post("/run", headers={"X-CLI-Token": "secret"},
                   json={"provider": "cmdcode", "prompt": "x"})
        assert r.json()["ok"] is True
        dirs.add(r.json()["response"])

    assert len(dirs) == 2
    for d in dirs:
        assert str(workspace) in d
    assert list(workspace.iterdir()) == []


def test_isolated_home_directory_for_cmd(tmp_path, monkeypatch):
    fake_user_home = tmp_path / "user-home"
    fake_cmd_dir = fake_user_home / ".commandcode"
    fake_cmd_dir.mkdir(parents=True)
    (fake_cmd_dir / "auth.json").write_text('{"token": "test-token"}')
    (fake_cmd_dir / "config.json").write_text('{"config": "test-config"}')

    monkeypatch.setenv("HOME", str(fake_user_home))

    stub = tmp_path / "stub-env"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        echo "$HOME"
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("CLI_TOKEN", "secret")
    monkeypatch.setenv("CMD_BIN", str(stub))
    workspace = tmp_path / "ws"
    monkeypatch.setenv("CLI_WORKSPACE_DIR", str(workspace))

    sys.modules.pop("main", None)
    import main
    monkeypatch.setattr(main.shutil, "rmtree", lambda *a, **k: None)
    c = TestClient(main.app)

    r = c.post("/run", headers={"X-CLI-Token": "secret"},
               json={"provider": "cmdcode", "prompt": "x"})
    assert r.json()["ok"] is True
    run_home = r.json()["response"].strip()

    assert run_home != str(fake_user_home)
    assert run_home.endswith("/home")

    isolated_cmd_dir = Path(run_home) / ".commandcode"
    assert (isolated_cmd_dir / "auth.json").read_text() == '{"token": "test-token"}'
    assert (isolated_cmd_dir / "config.json").read_text() == '{"config": "test-config"}'


def test_windows_shim_resolution_plain_argv(monkeypatch):
    monkeypatch.setattr(main, "IS_WINDOWS", False)
    argv = ["cmd", "-p", "analyze", "--yolo"]
    assert main._platform_windows_resolve(argv) == argv


def test_windows_shim_resolution_batch_file():
    assert main._platform_windows_resolve(["cmd.cmd", "-p", "hello"]) == [
        "cmd.exe", "/c", "cmd.cmd", "-p", "hello"]


def test_windows_shim_resolution_full_path_batch():
    assert main._platform_windows_resolve([r"C:\\Users\\x\\AppData\\Roaming\\npm\\cmd.cmd",
                                           "--prompt", "hi"]) == [
        "cmd.exe", "/c", r"C:\\Users\\x\\AppData\\Roaming\\npm\\cmd.cmd",
        "--prompt", "hi"]


def test_windows_shim_resolution_does_not_touch_exe_files(monkeypatch):
    monkeypatch.setattr(main.shutil, "which", lambda n: r"C:\\tools\\agy.exe")
    argv = ["agy", "--prompt", "hi"]
    assert main._platform_windows_resolve(argv) == argv


def test_health_reports_platform(monkeypatch):
    sys.modules.pop("main", None)
    import main
    monkeypatch.setenv("CLI_TOKEN", "secret")
    c = TestClient(main.app)
    assert c.get("/health").json()["platform"] in ("linux", "darwin", "windows")


def test_metrics_endpoint(client):
    # Execute a run to generate metrics
    client.post(
        "/run",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "cmdcode", "prompt": "test metrics"},
    )
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "cli_runner_requests_total" in r.text
    assert "cli_runner_active_processes" in r.text
    assert "cli_runner_duration_seconds" in r.text


def test_agent_router_endpoint(client):
    # Test high speed route -> low effort
    r = client.post(
        "/route",
        headers={"X-CLI-Token": "secret"},
        json={"task": "quick_fix", "requirements": {"speed": "high"}},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["effort"] == "low"
    assert data["provider"] in ("cmd", "agy")

    # Test high reasoning route -> high effort
    r2 = client.post(
        "/route",
        headers={"X-CLI-Token": "secret"},
        json={"task": "refactor_architecture", "requirements": {"reasoning": "high"}},
    )
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["effort"] == "high"


def test_run_with_auto_provider(client):
    r = client.post(
        "/run",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "auto", "task": "quick_fix", "prompt": "format this"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_streaming_endpoint(client):
    r = client.post(
        "/run/stream",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "cmdcode", "prompt": "stream test"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    content = r.text
    assert "event: queued" in content
    assert "event: start" in content
    assert "event: chunk" in content
    assert "event: done" in content


def test_job_queue_lifecycle(client):
    # Submit job
    r = client.post(
        "/jobs",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "cmdcode", "prompt": "job test"},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "queued"

    # Get job status
    r_get = client.get(f"/jobs/{job_id}", headers={"X-CLI-Token": "secret"})
    assert r_get.status_code == 200
    assert r_get.json()["id"] == job_id

    # List jobs
    r_list = client.get("/jobs", headers={"X-CLI-Token": "secret"})
    assert r_list.status_code == 200
    assert len(r_list.json()["jobs"]) >= 1

    # Cancel job
    r_cancel = client.delete(f"/jobs/{job_id}", headers={"X-CLI-Token": "secret"})
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "cancelled"


def test_ip_allowlist_restriction(tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_TOKEN", "secret")
    monkeypatch.setenv("CLI_ALLOWED_IPS", "192.168.1.100")
    sys.modules.pop("main", None)
    import main
    c = TestClient(main.app)

    r = c.post(
        "/run",
        headers={"X-CLI-Token": "secret"},
        json={"provider": "cmdcode", "prompt": "blocked"},
    )
    assert r.status_code == 403


def test_async_job_worker_execution(tmp_path, monkeypatch):
    stub = tmp_path / "stub-worker"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        echo '{"worker_done": true}'
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CLI_TOKEN", "secret")
    monkeypatch.setenv("CMD_BIN", str(stub))
    monkeypatch.setenv("CLI_WORKSPACE_DIR", str(tmp_path / "ws"))
    sys.modules.pop("main", None)
    import main

    with TestClient(main.app) as c:
        r = c.post(
            "/jobs",
            headers={"X-CLI-Token": "secret"},
            json={"provider": "cmdcode", "prompt": "worker execute"},
        )
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        for _ in range(50):
            time.sleep(0.05)
            job = c.get(f"/jobs/{job_id}", headers={"X-CLI-Token": "secret"}).json()
            if job["status"] in ("completed", "failed"):
                break

        assert job["status"] == "completed"
        assert "worker_done" in job["response"]


def test_job_not_found(client):
    r = client.get("/jobs/job-nonexistent", headers={"X-CLI-Token": "secret"})
    assert r.status_code == 404
    r_del = client.delete("/jobs/job-nonexistent", headers={"X-CLI-Token": "secret"})
    assert r_del.status_code == 404


def test_route_unauthorized(client):
    r = client.post("/route", json={"task": "refactor"})
    assert r.status_code == 401


def test_stream_unauthorized(client):
    r = client.post("/run/stream", json={"provider": "cmdcode", "prompt": "test"})
    assert r.status_code == 401

