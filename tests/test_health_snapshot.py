"""Health snapshot plumbing: the search path reads what the probe proved."""

from __future__ import annotations

import json
import time

import pytest

from josty import health


@pytest.fixture(autouse=True)
def _reset_snapshot_memory():
    health._SNAPSHOT_MEMORY.update({"at": 0.0, "path": None, "states": {}})
    yield
    health._SNAPSHOT_MEMORY.update({"at": 0.0, "path": None, "states": {}})


def _report(*pairs):
    return {"backends": [{"backend": name, "state": state} for name, state in pairs]}


def test_snapshot_round_trip_maps_states_to_error_kinds(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setenv("JOSTY_HEALTH_SNAPSHOT", str(path))
    health.save_snapshot(_report(("mojeek", "challenged"), ("yahoo", "ok")))

    assert health.load_snapshot()["mojeek"] == "challenged"
    assert health.known_error_kind("mojeek") == "blocked"
    assert health.known_error_kind("yahoo") is None
    assert health.known_error_kind("never-probed") is None


def test_rate_limited_and_js_required_map_to_distinct_kinds(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSTY_HEALTH_SNAPSHOT", str(tmp_path / "h.json"))
    health.save_snapshot(_report(("brave", "rate_limited"), ("google", "js_required")))

    assert health.known_error_kind("brave") == "rate_limited"
    assert health.known_error_kind("google") == "parse"


def test_stale_snapshot_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "stale.json"
    monkeypatch.setenv("JOSTY_HEALTH_SNAPSHOT", str(path))
    path.write_text(
        json.dumps({"generated_at": time.time() - 100000, "states": {"mojeek": "challenged"}})
    )

    assert health.load_snapshot() == {}
    assert health.known_error_kind("mojeek") is None


def test_corrupt_snapshot_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.json"
    monkeypatch.setenv("JOSTY_HEALTH_SNAPSHOT", str(path))
    path.write_text("{not json")

    assert health.load_snapshot() == {}


def test_absent_snapshot_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSTY_HEALTH_SNAPSHOT", str(tmp_path / "absent.json"))

    assert health.load_snapshot() == {}
    assert health.known_error_kind("yahoo") is None


def test_snapshot_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSTY_HEALTH_SNAPSHOT", str(tmp_path / "h.json"))

    def boom(report, path=None):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(health, "save_snapshot", boom)

    def fetch(url: str):
        return 200, "<html><title>Captcha</title></html>", 1.0

    report = health.run_health(["mojeek"], fetch=fetch, save=True)

    assert report["status"] == "complete"
    assert report["snapshot"] is None
    assert "Read-only" in report["snapshot_error"]


def test_run_health_does_not_write_unless_asked(tmp_path, monkeypatch):
    path = tmp_path / "never.json"
    monkeypatch.setenv("JOSTY_HEALTH_SNAPSHOT", str(path))

    def fetch(url: str):
        return 200, "<html><title>Captcha</title></html>", 1.0

    health.run_health(["mojeek"], fetch=fetch)
    assert not path.exists()

    health.run_health(["mojeek"], fetch=fetch, save=True)
    assert path.exists()
    assert health.known_error_kind("mojeek") == "blocked"
