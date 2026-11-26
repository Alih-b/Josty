"""The CLI does not wait for threads a timeout abandoned (#127). Hermetic."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from josty import cli


def test_main_prints_without_joining_the_default_executor(monkeypatch, capsys):
    """A lookup thread that outlives its budget must not hold the output."""
    release = threading.Event()

    async def run(args):
        import asyncio

        asyncio.get_running_loop().run_in_executor(None, release.wait, 30)
        return {"status": "complete"}

    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr("sys.argv", ["josty", "term"])
    started = time.perf_counter()
    try:
        cli.main()
        elapsed = time.perf_counter() - started
    finally:
        release.set()

    assert elapsed < 5
    assert capsys.readouterr().out.strip() == '{\n  "status": "complete"\n}'


def test_entry_leaves_at_once_when_threads_are_still_running(monkeypatch):
    left = []

    def fake_exit(code):
        left.append(code)
        raise RuntimeError("os._exit")

    def failing_main():
        raise SystemExit(1)

    monkeypatch.setattr(cli, "main", failing_main)
    monkeypatch.setattr(cli, "_threads_still_running", lambda: True)
    monkeypatch.setattr(cli.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="os._exit"):
        cli.entry()
    assert left == [1]


@pytest.mark.parametrize("code", [None, 0, 2])
def test_entry_returns_the_exit_code_normally_without_threads(monkeypatch, code):
    def finishing_main():
        raise SystemExit(code)

    monkeypatch.setattr(cli, "main", finishing_main)
    monkeypatch.setattr(cli, "_threads_still_running", lambda: False)
    with pytest.raises(SystemExit) as exit_info:
        cli.entry()
    assert exit_info.value.code == (code or 0)


def test_entry_keeps_a_message_exit_for_the_interpreter(monkeypatch):
    def message_main():
        raise SystemExit("usage: ...")

    monkeypatch.setattr(cli, "main", message_main)
    monkeypatch.setattr(cli, "_threads_still_running", lambda: True)
    with pytest.raises(SystemExit) as exit_info:
        cli.entry()
    assert exit_info.value.code == "usage: ..."


def test_a_process_with_a_stuck_thread_exits_when_the_output_is_written():
    script = (
        "import threading, time\n"
        "from josty import cli\n"
        "threading.Thread(target=time.sleep, args=(30,)).start()\n"
        "def fake_main():\n"
        "    print('{}')\n"
        "    raise SystemExit(3)\n"
        "cli.main = fake_main\n"
        "cli.entry()\n"
    )
    started = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=25
    )
    assert time.perf_counter() - started < 15
    assert (done.returncode, done.stdout.strip()) == (3, "{}")


def test_threads_still_running_sees_a_live_worker():
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    worker.start()
    try:
        assert cli._threads_still_running() is True
    finally:
        release.set()
        worker.join()
