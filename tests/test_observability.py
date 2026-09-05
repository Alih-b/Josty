"""Contract tests for coverage, fanout, SERP-cache, fetch-phase, and diagnose transport.

These pin the audit follow-up: agents must see how many providers actually
contributed, how many upstream calls a command scheduled, that fetch reuses a
SERP cache, that a total fetch miss is not ``complete``, and that ``--diagnose``
is an HTTPS-host probe rather than search health.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from josty.engine import (
    Josty,
    ProviderStatus,
    SearchResult,
    SearchRun,
    _search_run_from_dict,
    _strip_fetch_fields,
)


def _result(url: str, source: str = "brave") -> SearchResult:
    return SearchResult("title", url, "snippet", sources=[source])


class FakeDDGS:
    def __init__(self, **kwargs):
        pass

    def text(self, *args, **kwargs):
        return [{"title": "Hit", "href": "https://example.com/doc", "body": "Snippet"}]


def test_coverage_fields_distinguish_brave_only_complete():
    brave = ProviderStatus("brave", "q", True, 5)
    empty = [
        ProviderStatus(name, "q", True, 0, error_kind="empty")
        for name in ("duckduckgo", "google", "mojeek", "startpage", "yahoo")
    ]
    run = SearchRun("q", [_result("https://example.com/a")], [brave, *empty])
    payload = run.dict()
    assert payload["status"] == "complete"
    assert payload["provider_count"] == 6
    assert payload["nonempty_provider_count"] == 1
    assert payload["coverage"] == 0.167
    assert payload["fetch"]["status"] == "skipped"


def test_fetch_total_miss_degrades_run_and_exposes_counters():
    ok = ProviderStatus("brave", "q", True, 3)
    run = SearchRun(
        "q",
        [
            _result("https://example.com/a"),
            _result("https://example.com/b"),
            _result("https://example.com/c"),
        ],
        [ok],
        fetch_requested=True,
        fetch_attempted=3,
        fetch_ok=0,
        fetch_failed=3,
    )
    payload = run.dict()
    assert payload["status"] == "degraded"
    assert payload["partial"] is True
    assert payload["fetch"] == {
        "requested": True,
        "attempted": 3,
        "ok": 0,
        "failed": 3,
        "status": "failed",
    }


def test_partial_fetch_success_keeps_search_complete():
    ok = ProviderStatus("brave", "q", True, 2)
    run = SearchRun(
        "q",
        [_result("https://example.com/a"), _result("https://example.com/b")],
        [ok],
        fetch_requested=True,
        fetch_attempted=2,
        fetch_ok=1,
        fetch_failed=1,
    )
    payload = run.dict()
    assert payload["status"] == "complete"
    assert payload["fetch"]["status"] == "degraded"
    assert payload["fetch"]["ok"] == 1
    assert payload["fetch"]["failed"] == 1


def test_oss_two_sites_schedules_48_requests():
    engine = Josty()
    variants, requests = engine._fanout_telemetry(
        "document indexing",
        sites=["github.com", "gitlab.com"],
        mode="oss",
        category="text",
        include_github=False,
        max_query_variants=None,
    )
    assert variants == 8
    assert requests == 48


def test_exact_mode_doubles_scheduled_requests():
    engine = Josty()
    plain_v, plain_n = engine._fanout_telemetry(
        "q",
        sites=[],
        mode="plain",
        category="text",
        include_github=False,
        max_query_variants=None,
    )
    exact_v, exact_n = engine._fanout_telemetry(
        "q",
        sites=[],
        mode="exact",
        category="text",
        include_github=False,
        max_query_variants=None,
    )
    assert plain_v == 1
    assert exact_v == 2
    assert exact_n == plain_n * 2
    assert exact_n == 12


def test_github_opt_in_adds_one_scheduled_request():
    engine = Josty(backends=("brave",))
    _, without_gh = engine._fanout_telemetry(
        "q",
        sites=[],
        mode="plain",
        category="text",
        include_github=False,
        max_query_variants=None,
    )
    _, with_gh = engine._fanout_telemetry(
        "q",
        sites=[],
        mode="plain",
        category="text",
        include_github=True,
        max_query_variants=None,
    )
    assert without_gh == 1
    assert with_gh == 2


def test_research_run_stamps_fanout_and_coverage(monkeypatch):
    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    engine = Josty(backends=("brave", "duckduckgo"), enable_cache=False)
    run = asyncio.run(engine.search_run("alpha", mode="exact", limit=3))
    payload = run.dict()
    assert payload["query_variant_count"] == 2
    assert payload["request_count"] == 4
    assert payload["provider_count"] == 2
    assert payload["nonempty_provider_count"] == 2
    assert payload["coverage"] == 1.0
    assert payload["fetch"]["requested"] is False


def test_search_then_fetch_reuses_serp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    engine = Josty(backends=("brave",), cache_db=tmp_path / "c.db")
    calls = {"n": 0}
    orig = engine._ddgs

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await orig(*args, **kwargs)

    engine._ddgs = counting

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def fake_download(_self, client, url):
        return "<html><body><p>extracted page</p></body></html>", url

    monkeypatch.setattr(Josty, "_download", fake_download)

    first = asyncio.run(engine.search_run("cache fetch identity", limit=1, fetch=False))
    assert first.cached is False
    assert calls["n"] == 1
    assert first.dict()["fetch"]["requested"] is False

    second = asyncio.run(engine.search_run("cache fetch identity", limit=1, fetch=True))
    assert second.cached is True
    assert calls["n"] == 1
    assert second.results[0].content
    payload = second.dict()
    assert payload["fetch"]["requested"] is True
    assert payload["fetch"]["attempted"] == 1
    assert payload["fetch"]["ok"] == 1
    assert payload["fetch"]["failed"] == 0
    assert payload["fetch"]["status"] == "complete"
    assert payload["status"] == "complete"


def test_cached_payload_does_not_store_fetch_phase(tmp_path, monkeypatch):
    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    engine = Josty(backends=("brave",), cache_db=tmp_path / "c.db")

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def fake_download(_self, client, url):
        return "<html><body><p>page</p></body></html>", url

    monkeypatch.setattr(Josty, "_download", fake_download)

    asyncio.run(engine.search_run("serp only", limit=1, fetch=True))
    with sqlite3.connect(str(engine.cache.db_path)) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM search_cache").fetchone()[0])
    assert payload["results"][0]["content"] is None
    assert payload["fetch"] == {
        "requested": False,
        "attempted": 0,
        "ok": 0,
        "failed": 0,
        "status": "skipped",
    }
    assert payload["query_variant_count"] == 1
    assert payload["request_count"] == 1


def test_live_fetch_total_miss_marks_degraded(monkeypatch):
    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    engine = Josty(backends=("brave",), enable_cache=False)

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def fake_download(_self, client, url):
        raise ValueError("blocked")

    monkeypatch.setattr(Josty, "_download", fake_download)

    run = asyncio.run(engine.search_run("fetch miss", limit=1, fetch=True))
    payload = run.dict()
    assert payload["status"] == "degraded"
    assert payload["fetch"]["requested"] is True
    assert payload["fetch"]["attempted"] == 1
    assert payload["fetch"]["ok"] == 0
    assert payload["fetch"]["failed"] == 1
    assert payload["fetch"]["status"] == "failed"


def test_diagnose_envelope_is_transport_specific(monkeypatch):
    import httpx

    async def fake_get(self, url, **kwargs):
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(Josty(backends=("brave",)).diagnose_run()).dict()
    assert payload["phase"] == "transport"
    assert payload["probe"] == "https_host"
    assert "not search-backend health" in payload["note"]
    assert payload["status"] == "complete"
    assert payload["providers"][0]["ok"] is True
    assert payload["providers"][0]["challenged"] is True
    assert payload["providers"][0]["http_status"] == 429


def test_strip_fetch_fields_resets_phase_block():
    run = SearchRun(
        "q",
        [_result("https://example.com/a")],
        [ProviderStatus("brave", "q", True, 1)],
        fetch_requested=True,
        fetch_attempted=1,
        fetch_ok=1,
        fetch_failed=0,
    )
    run.results[0].content = "page text"
    payload = _strip_fetch_fields(run.dict())
    assert payload["results"][0]["content"] is None
    assert payload["fetch"]["status"] == "skipped"
    restored = _search_run_from_dict(payload)
    assert restored.fetch_requested is False
    assert restored.fetch_status == "skipped"


def test_legacy_payload_without_new_fields_still_loads():
    restored = _search_run_from_dict(
        {
            "query": "legacy",
            "providers": [{"provider": "brave", "query": "legacy", "ok": True, "result_count": 1}],
            "results": [{"title": "t", "url": "https://example.com/x", "snippet": ""}],
        }
    )
    payload = restored.dict()
    assert payload["query_variant_count"] == 0
    assert payload["request_count"] == 0
    assert payload["nonempty_provider_count"] == 1
    assert payload["fetch"]["status"] == "skipped"
