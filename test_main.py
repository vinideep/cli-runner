"""Tests for the cli-runner service. Uses a stub 'cmd' script so no real CLI or
auth is needed."""

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

import main  # noqa: E402  (helper functions; fixtures re-import with patched env)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # A stub binary that ignores its args and prints a known JSON array, so we
    # exercise the service's subprocess + parsing without a real CLI.
    stub = tmp_path / "stub-cmd"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        echo '[{"ok":true}]'
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("CLI_TOKEN", "secret")
    monkeypatch.setenv("CMD_BIN", str(stub))
    monkeypatch.setenv("AGY_BIN", str(stub))
    monkeypatch.setenv("CLI_WORKSPACE_DIR", str(tmp_path / "ws"))

    # Import fresh so module-level env reads pick up the monkeypatched values.
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
    # CHG-151: live queue-depth gauges — the backend clamps its batch
    # concurrency to max_concurrency, and in_flight/waiting make queued
    # requests visible (uvicorn's access log only shows completions).
    assert body["in_flight"] == 0
    assert body["waiting"] == 0
    assert body["max_concurrency"] >= 1


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
    """CHG-149: the old default of 1 serialised every CLI call — a full batch
    pass (dozens of prompts) ran one-at-a-time and took hours. Default must now
    allow more than one CLI process at once."""
    body = client.get("/health").json()
    assert body["max_concurrency"] >= 3


def test_argv_never_includes_continuation_flag():
    """CHG-147/149: a continued session is what made cmd reply with a prose
    summary instead of JSON. Every invocation this service builds must be a
    fresh session — assert no continuation flag ever appears, for either CLI."""
    sys.modules.pop("main", None)
    import main
    for kind in ("cmd", "agy"):
        argv = main._build_argv(kind, "some prompt", model=None, effort=None)
        assert "-c" not in argv
        assert "--continue" not in argv


def test_effort_flag_added_when_requested():
    """--effort is the main latency lever: low makes the CLI run faster (less
    reasoning), high is slower but deeper. The service must pass it through."""
    sys.modules.pop("main", None)
    import main
    for kind in ("cmd", "agy"):
        argv = main._build_argv(kind, "prompt", model=None, effort="low")
        assert "--effort" in argv
        assert argv[argv.index("--effort") + 1] == "low"


def test_effort_not_added_by_default():
    """No effort flag when the caller doesn't ask for one — let the CLI use its
    own default reasoning effort."""
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
    """With MAX_CONCURRENCY > 1, multiple CLI processes can run at the same
    time — each run must get its OWN cwd so a coding-agent CLI's scratch files
    can never collide with another run's, and the scratch dir must be cleaned
    up afterward (no buildup across a large batch pass)."""
    workspace = tmp_path / "ws"
    stub = tmp_path / "stub-pwd"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        pwd
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

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

    assert len(dirs) == 2  # two distinct scratch dirs, one per run
    for d in dirs:
        assert str(workspace) in d
    # Cleaned up afterward — nothing left behind in the workspace root.
    assert list(workspace.iterdir()) == []


def test_isolated_home_directory_for_cmd(tmp_path, monkeypatch):
    """Verify that cli-runner sets isolated HOME environment and copies config files for cmd."""
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
    """POSIX-style argv must flow through unchanged on non-Windows platforms."""
    monkeypatch.setattr(main, "IS_WINDOWS", False)
    argv = ["cmd", "-p", "analyze", "--yolo"]
    assert main._platform_windows_resolve(argv) == argv


def test_windows_shim_resolution_batch_file():
    """A .cmd shim on Windows must be executed via the system shell."""
    assert main._platform_windows_resolve(["cmd.cmd", "-p", "hello"]) == [
        "cmd.exe", "/c", "cmd.cmd", "-p", "hello"]


def test_windows_shim_resolution_full_path_batch():
    """An absolute path to a .cmd shim must also go through cmd.exe /c."""
    assert main._platform_windows_resolve([r"C:\\Users\\x\\AppData\\Roaming\\npm\\cmd.cmd",
                                           "--prompt", "hi"]) == [
        "cmd.exe", "/c", r"C:\\Users\\x\\AppData\\Roaming\\npm\\cmd.cmd",
        "--prompt", "hi"]


def test_windows_shim_resolution_does_not_touch_exe_files(monkeypatch):
    """A real .exe (e.g. agy.exe) is launched directly, never via cmd.exe."""
    monkeypatch.setattr(main.shutil, "which", lambda n: r"C:\\tools\\agy.exe")
    argv = ["agy", "--prompt", "hi"]
    assert main._platform_windows_resolve(argv) == argv


def test_health_reports_platform(monkeypatch):
    """The startup OS detection must be visible via /health so the backend (and
    an operator) can see which OS-specific behaviour is active."""
    sys.modules.pop("main", None)
    import main
    monkeypatch.setenv("CLI_TOKEN", "secret")
    c = TestClient(main.app)
    assert c.get("/health").json()["platform"] in ("linux", "darwin", "windows")
