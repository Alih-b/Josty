"""Unit tests for the thin Josty launcher delegator."""

import importlib.util
from pathlib import Path

import pytest

LAUNCHER_PATH = (
    Path(__file__).parents[1] / ".agents" / "skills" / "josty" / "scripts" / "run.py"
)
SPEC = importlib.util.spec_from_file_location("josty_launcher", LAUNCHER_PATH)
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


def test_ensure_josty_skips_install_when_console_script_present(monkeypatch):
    def fake_which(cmd):
        return "/usr/bin/josty" if cmd == "josty" else None

    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)
    launcher._ensure_josty()
    assert calls == []


def test_ensure_josty_uses_uv_tool_install(monkeypatch):
    def fake_which(cmd):
        return {"uv": "/usr/bin/uv", "pipx": None, "josty": None}.get(cmd)

    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)
    launcher._ensure_josty()
    assert calls == [["/usr/bin/uv", "tool", "install", "josty"]]


def test_ensure_josty_uses_pipx_when_no_uv(monkeypatch):
    def fake_which(cmd):
        return {"uv": None, "pipx": "/usr/bin/pipx", "josty": None}.get(cmd)

    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)
    launcher._ensure_josty()
    assert calls == [["/usr/bin/pipx", "install", "josty"]]


def test_ensure_josty_falls_back_to_pip_user(monkeypatch):
    def fake_which(cmd):
        return None

    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)
    launcher._ensure_josty()
    assert calls == [
        [launcher.sys.executable, "-m", "pip", "install", "--user", "josty"]
    ]


def test_main_runs_josty_with_args(monkeypatch):
    def fake_which(cmd):
        return "/usr/bin/josty" if cmd == "josty" else None

    calls = []

    def fake_call(cmd, *args, **kwargs):
        calls.append(cmd)
        return 3

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr(launcher.sys, "argv", ["run.py", "--limit", "3", "query"])
    monkeypatch.setattr(launcher.subprocess, "call", fake_call)

    with pytest.raises(SystemExit) as exc_info:
        launcher.main()
    assert exc_info.value.code == 3
    assert calls == [["josty", "--limit", "3", "query"]]
