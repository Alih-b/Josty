"""The breaker's memory (#105).

One failure suspends an engine, the next process honours it, a second failure
doubles the wait, and an answer clears it. Hermetic: the ddgs double never
leaves the process (AGENTS.md invariant 2).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from ddgs.exceptions import RatelimitException

from josty.engine import Josty


class _Refusing:
    """Fails every call the way a blocked engine does."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def __call__(self, **kwargs):
        return self

    def text(self, query, **kwargs):
        self.calls += 1
        raise self._exc


class _Hanging:
    """Never answers inside the budget: the branch reports a timeout."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs):
        return self

    def text(self, query, **kwargs):
        self.calls += 1
        time.sleep(1.0)
        return []


class _Answering:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs):
        return self

    def text(self, query, **kwargs):
        self.calls += 1
        return [{"title": "Answer", "href": "https://example.org/answer"}]


def _search(engine: Josty):
    return asyncio.run(engine.search_run("q", limit=5))


def _learned(engine: Josty) -> tuple[float, int] | None:
    return engine.cache.open_circuits(60.0).get("brave")


def _cool_down_just_ended(cache) -> None:
    """Move the stored deadline to one second ago, keeping its trip count."""
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE breaker_state SET until_utc = ?", (time.time() - 1.0,))


@pytest.mark.parametrize("failure", ["429", "timeout"])
def test_a_suspension_is_remembered_doubled_and_forgotten(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("josty.engine.SEARCH_THREAD_TIMEOUT_HEADROOM", 0.1)
    cache_db = tmp_path / "cache.db"

    def failing():
        return _Refusing(RatelimitException("429")) if failure == "429" else _Hanging()

    def process(ddgs) -> Josty:
        monkeypatch.setattr("josty.engine.DDGS", ddgs)
        return Josty(backends=("brave",), cache_db=cache_db, timeout=0.2)

    # Process A: one failure is enough.
    engine_a = process(failing())
    expected = "rate_limited" if failure == "429" else "network"
    assert _search(engine_a).providers[0].error_kind == expected
    until, trips = _learned(engine_a)
    assert 170 < until - time.time() <= 180
    assert trips == 1

    # Process B, inside the cool-down: the engine is not asked at all.
    guard = _Refusing(AssertionError("a suspended engine must not be called"))
    engine_b = process(guard)
    run = _search(engine_b)
    assert guard.calls == 0
    assert run.providers[0].error_kind == "skipped"
    assert "cool-down until" in run.providers[0].error

    # Process C, just after the cool-down: it fails again, and the wait doubles.
    _cool_down_just_ended(engine_b.cache)
    engine_c = process(failing())
    _search(engine_c)
    until, trips = _learned(engine_c)
    assert 350 < until - time.time() <= 360
    assert trips == 2

    # Process D: the engine answers, and nothing is left to remember.
    _cool_down_just_ended(engine_c.cache)
    answering = _Answering()
    engine_d = process(answering)
    assert _search(engine_d).results
    assert answering.calls == 1
    assert _learned(engine_d) is None


def test_history_that_has_decayed_is_dropped(tmp_path):
    from josty.cache import SearchCache

    cache = SearchCache(tmp_path / "old.db", default_ttl=60.0)
    cache.note_trip("brave", time.time() - 1000.0, 1)
    cache.note_trip("mojeek", time.time() - 5.0, 1)

    assert set(cache.open_circuits(60.0)) == {"mojeek"}


def test_a_restored_deadline_is_capped_by_its_own_cool_down(tmp_path):
    """A clock that stepped back must not stretch a saved trip past its cap (#114)."""
    from josty.breaker import CircuitBreaker

    breaker = CircuitBreaker(cool_down_seconds=180.0, max_cool_down_seconds=360.0)
    breaker.restore("brave", time.time() + 86400.0, 1)

    allowed, message = breaker.status("brave")
    assert allowed is False
    assert "cool-down until" in message
    assert breaker.get_state("brave")["backoff_remaining"] <= 180.0


def test_a_local_outage_is_not_saved_for_the_next_process(tmp_path, monkeypatch):
    """Every engine failing at the network is the machine, not the engines (#114)."""
    import httpx

    monkeypatch.setattr(
        "josty.engine.DDGS", _Refusing(httpx.ConnectError("no route to host"))
    )
    engine = Josty(backends=("brave,duckduckgo",), cache_db=tmp_path / "off.db", timeout=0.2)
    run = _search(engine)

    assert {provider.error_kind for provider in run.providers} == {"network"}
    # Nothing is left for the next process...
    assert engine.cache.open_circuits(60.0) == {}
    # ...but this process still leaves both engines alone.
    assert engine.breaker.status("brave")[0] is False


def test_a_parse_error_leaves_the_circuit_closed(tmp_path, monkeypatch):
    """An unreadable page is reported, but it neither trips nor persists a trip (#124)."""
    from ddgs.exceptions import DDGSException

    cache_db = tmp_path / "cache.db"
    unreadable = _Refusing(DDGSException("could not parse the results page"))
    monkeypatch.setattr("josty.engine.DDGS", unreadable)
    engine = Josty(backends=("brave",), cache_db=cache_db, timeout=0.2)

    first = _search(engine)
    assert first.providers[0].error_kind == "parse"
    assert first.providers[0].ok is False
    assert _learned(engine) is None

    # The same process asks again, and so does the next one.
    _search(engine)
    assert unreadable.calls == 2
    monkeypatch.setattr("josty.engine.DDGS", unreadable)
    _search(Josty(backends=("brave",), cache_db=cache_db, timeout=0.2))
    assert unreadable.calls == 3


def test_blocked_still_trips_the_circuit(tmp_path, monkeypatch):
    monkeypatch.setattr("josty.engine.DDGS", _Refusing(RatelimitException("429")))
    engine = Josty(backends=("brave",), cache_db=tmp_path / "cache.db", timeout=0.2)
    _search(engine)
    assert _learned(engine) is not None
