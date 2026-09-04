import asyncio
import socket
import sqlite3
import ssl
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from josty.engine import (
    CircuitBreaker,
    Josty,
    ProviderStatus,
    SearchCache,
    SearchResult,
    SearchRun,
    _aggregate_engine_status,
    _content_type_allowed,
    _engine_available,
    _search_run_from_dict,
    _ttl_for,
    canonical,
    domain_weight,
    merge_query_variants,
    normalize_sites,
    rrf,
)


@pytest.fixture(autouse=True)
def isolate_test_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def result(url, snippet="", source="test"):
    return SearchResult("title", url, snippet, sources=[source])


def _dns_connect_error():
    exc = httpx.ConnectError("dns failure", request=httpx.Request("GET", "u"))
    exc.__cause__ = socket.gaierror(-2, "Name or service not known")
    return exc


def _tls_connect_error():
    exc = httpx.ConnectError("tls failure", request=httpx.Request("GET", "u"))
    exc.__cause__ = ssl.SSLError(1, "handshake failure")
    return exc


def test_canonical_preserves_resource_query_and_removes_tracking():
    assert canonical("HTTPS://Example.COM:443/path/?utm_source=x#part") == "https://example.com/path"
    assert canonical(
        "https://example.com/search?q=python&utm_campaign=test&page=2&fbclid=abc"
    ) == "https://example.com/search?q=python&page=2"
    assert canonical("https://example.com/search?q=rust") != canonical(
        "https://example.com/search?q=python"
    )
    assert canonical("http://Example.com:8080/") == "http://example.com:8080/"
    assert canonical("https://[2606:4700:4700::1111]/") == "https://[2606:4700:4700::1111]/"
    with pytest.raises(ValueError):
        canonical("relative/path")


def test_rrf_fuses_independent_lists_and_preserves_provenance():
    first = [result("https://example.com/a?utm_source=test", "short", "one")]
    second = [result("https://EXAMPLE.com/a/", "longer snippet", "two")]
    results = rrf([first, second])
    assert len(results) == 1
    assert results[0].snippet == "longer snippet"
    assert results[0].sources == ["one", "two"]
    # Per-engine attribution contract: score derives from the rounded
    # per-engine contributions, not from unrounded list-position terms.
    assert results[0].score == round(2 * round(1 / 61, 6), 6)
    assert first[0].score == 0.0  # fusion does not mutate caller-owned results
    assert first[0].engine_ranks == {}
    assert second[0].engine_ranks == {}


def test_rrf_rejects_invalid_k_and_malformed_urls():
    with pytest.raises(ValueError):
        rrf([], k=0)
    assert rrf([[result("https://example.com:invalid/path")]]) == []


def test_query_variants_do_not_create_multiple_provider_votes():
    merged = merge_query_variants(
        [
            [result("https://example.com/a", source="backend")],
            [
                result("https://example.com/b", source="backend"),
                result("https://example.com/a", "better", "backend"),
            ],
        ]
    )
    assert [item.url for item in merged] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert merged[0].snippet == "better"
    assert merged[0].sources == ["backend"]


def test_query_modes_are_explicit():
    assert Josty.expand("company revenue history") == ["company revenue history"]
    assert Josty.expand("agent search", mode="exact") == [
        "agent search",
        '"agent search"',
    ]
    assert Josty.expand("agent search", mode="oss") == [
        "agent search",
        '"agent search"',
        "agent search open source",
        "agent search self-hosted",
    ]


def test_site_filters_scope_every_variant():
    assert Josty.expand("agent search", ["GitHub.com"], mode="exact") == [
        "site:github.com agent search",
        'site:github.com "agent search"',
    ]
    assert normalize_sites(["www.GitHub.com", "github.com"]) == ["github.com"]


@pytest.mark.parametrize(
    "site",
    [
        "https://github.com",
        "github.com/path",
        "bad host",
        "x:y",
        ".github.com",
        "-bad.example",
        "bad-.example",
    ],
)
def test_invalid_site_filters_are_rejected(site):
    with pytest.raises(ValueError, match="invalid site"):
        normalize_sites([site])


def test_site_filter_count_is_bounded():
    with pytest.raises(ValueError, match="at most"):
        normalize_sites([f"site{i}.test" for i in range(6)])


def test_site_results_are_post_filtered(monkeypatch):
    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            return [
                {"title": "inside", "href": "https://docs.github.com/a", "body": ""},
                {"title": "outside", "href": "https://example.com/a", "body": ""},
            ]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    run = asyncio.run(
        Josty(backends=("duckduckgo",)).search_run("query", sites=["github.com"], limit=3)
    )
    assert [item.url for item in run.results] == ["https://docs.github.com/a"]


def test_invalid_search_options_and_content_bounds_are_rejected():
    with pytest.raises(ValueError):
        asyncio.run(Josty().search_run("query", limit=101))
    with pytest.raises(ValueError, match="category"):
        asyncio.run(Josty().search_run("query", category="invalid"))
    with pytest.raises(ValueError, match="safe-search"):
        asyncio.run(Josty().search_run("query", safesearch="invalid"))
    with pytest.raises(ValueError, match="time limit"):
        asyncio.run(Josty().search_run("query", timelimit="invalid"))
    with pytest.raises(ValueError):
        Josty(max_download_bytes=0)


def test_search_and_fetch_concurrency_have_independent_defaults():
    assert Josty.DEFAULT_SEARCH_CONCURRENCY == 6
    assert Josty.DEFAULT_FETCH_CONCURRENCY == 4
    engine = Josty()
    assert engine.max_search_concurrency == 6
    assert engine.max_fetch_concurrency == 4
    search_sem = engine._search_semaphore()
    fetch_sem = engine._fetch_semaphore()
    assert search_sem is not fetch_sem


def test_concurrency_constructor_params_are_validated():
    with pytest.raises(ValueError):
        Josty(max_search_concurrency=0)
    with pytest.raises(ValueError):
        Josty(max_fetch_concurrency=0)


def test_max_concurrency_alias_targets_search_slot():
    engine = Josty(max_concurrency=3)
    assert engine.max_search_concurrency == 3
    assert engine.max_fetch_concurrency == Josty.DEFAULT_FETCH_CONCURRENCY


def test_search_and_fetch_semaphores_do_not_share_a_pool():
    observed = {"peak_search": 0, "peak_fetch": 0}
    counters = {"search": 0, "fetch": 0}

    async def hold(sem: asyncio.Semaphore, kind: str) -> None:
        async with sem:
            counters[kind] += 1
            observed[f"peak_{kind}"] = max(observed[f"peak_{kind}"], counters[kind])
            await asyncio.sleep(0)
            counters[kind] -= 1

    async def scenario() -> None:
        engine = Josty(max_search_concurrency=2, max_fetch_concurrency=3)
        search_sem = engine._search_semaphore()
        fetch_sem = engine._fetch_semaphore()
        await asyncio.gather(
            *(hold(search_sem, "search") for _ in range(2)),
            *(hold(fetch_sem, "fetch") for _ in range(3)),
        )

    asyncio.run(scenario())
    assert observed["peak_search"] == 2
    assert observed["peak_fetch"] == 3


def test_search_run_status():
    ok = ProviderStatus("one", "q", True, 1)
    failed = ProviderStatus("two", "q", False, error="blocked")
    assert SearchRun("q", [result("https://example.com")], [ok]).status == "complete"
    assert SearchRun("q", [result("https://example.com")], [ok, failed]).status == "degraded"
    assert SearchRun("q", [], [failed]).status == "failed"
    assert SearchRun("q", [], [ok, failed]).status == "degraded"
    assert SearchRun("q", [], [ok]).status == "complete"
    assert SearchRun("q", [], [ok]).dict()["schema_version"] == "1.0"
    assert SearchRun("q", [], [ok]).dict()["cached"] is False


@pytest.mark.parametrize(
    "address, family",
    [
        ("127.0.0.1", socket.AF_INET),
        ("169.254.169.254", socket.AF_INET),
        ("10.0.0.1", socket.AF_INET),
        ("::1", socket.AF_INET6),
    ],
)
def test_ssrf_guard_blocks_reserved_addresses(monkeypatch, address, family):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 80))],
    )
    with pytest.raises(ValueError, match="private or reserved"):
        asyncio.run(Josty()._validate_public_url("http://example.test"))


def test_ssrf_guard_blocks_unsafe_schemes_and_credentials():
    with pytest.raises(ValueError, match="HTTP"):
        asyncio.run(Josty()._validate_public_url("file:///etc/passwd"))
    with pytest.raises(ValueError, match="credentials"):
        asyncio.run(Josty()._validate_public_url("https://user:secret@example.com"))


def test_public_network_is_allowed(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        ],
    )
    asyncio.run(Josty()._validate_public_url("https://example.com"))


def test_download_limit_is_enforced(monkeypatch):
    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def download():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"too large"
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await Josty(max_download_bytes=3)._download(client, "https://example.com")

    with pytest.raises(ValueError, match="download limit"):
        asyncio.run(download())


def test_unsupported_content_type_is_blocked(monkeypatch):
    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def download():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/octet-stream"}, content=b"data"
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await Josty()._download(client, "https://example.com")

    with pytest.raises(ValueError, match="unsupported content type"):
        asyncio.run(download())


def test_content_type_charset_parameter_is_allowed(monkeypatch):
    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def download():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html>ok</html>",
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await Josty()._download(client, "https://example.com")

    body, _url = asyncio.run(download())
    assert "ok" in body


@pytest.mark.parametrize(
    "header",
    ["", None, "application/pdf; x=text/html", "application/json"],
)
def test_content_type_empty_or_spoofed_is_rejected(monkeypatch, header):
    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def download():
        headers = {} if header is None else {"content-type": header}
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, content=b"%PDF")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await Josty()._download(client, "https://example.com")

    with pytest.raises(ValueError, match="unsupported content type"):
        asyncio.run(download())


def test_content_type_allowed_helper():
    assert _content_type_allowed("text/html; charset=utf-8") is True
    assert _content_type_allowed("application/xhtml+xml") is True
    assert _content_type_allowed("text/plain") is True
    assert _content_type_allowed("") is False
    assert _content_type_allowed(None) is False
    assert _content_type_allowed("application/pdf; x=text/html") is False


def test_empty_trafilatura_result_uses_safe_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace(extract=lambda *a, **k: None))
    content, method = Josty._extract(
        "<html><script>ignore()</script><main>Hello world</main></html>",
        "https://example.com",
    )
    assert content == "Hello world"
    assert method == "html-text-fallback"


def test_known_ad_redirects_are_filtered_without_substring_false_positive():
    assert Josty._is_ad_redirect("https://www.google.com/aclick?id=1")
    assert Josty._is_ad_redirect("https://ad.doubleclick.net/path")
    assert not Josty._is_ad_redirect(
        "https://example.com/article?topic=doubleclick.net"
    )
    assert not Josty._is_ad_redirect("https://notgoogle.com/aclick?id=1")


def test_provider_failures_are_reported_without_hidden_retry(monkeypatch):
    class BrokenDDGS:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            self.__class__.calls += 1
            raise RuntimeError("blocked")

    monkeypatch.setattr("josty.engine.DDGS", BrokenDDGS)
    run = asyncio.run(Josty(backends=("duckduckgo",)).search_run("query", limit=3))
    assert run.results == []
    assert run.status == "failed"
    assert run.providers[0].provider == "duckduckgo"
    assert "RuntimeError: blocked" in run.providers[0].error
    assert BrokenDDGS.calls == 1


def test_duplicate_engine_names_within_a_group_are_deduped(monkeypatch):
    seen_backends = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            seen_backends.append(kwargs["backend"])
            return [{"title": "Hit", "href": "https://example.com/a", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    asyncio.run(Josty(backends=("duckduckgo,duckduckgo",)).search_run("query", limit=5))
    assert seen_backends == ["duckduckgo"]


def test_group_backends_fan_out_per_engine_and_stay_group_fused(monkeypatch):
    seen_backends = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            seen_backends.append(kwargs["backend"])
            return [
                {
                    "title": f"Hit via {kwargs['backend']}",
                    "href": "https://example.com/a",
                    "body": "Snippet",
                }
            ]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    run = asyncio.run(
        Josty(backends=("duckduckgo,brave", "google")).search_run("query", limit=5)
    )
    assert sorted(seen_backends) == ["brave", "duckduckgo", "google"]
    assert {status.provider for status in run.providers} == {"duckduckgo", "brave", "google"}
    assert all(status.ok for status in run.providers)
    assert len(run.results) == 1
    assert run.results[0].sources == ["duckduckgo", "brave", "google"]


def test_provider_status_is_one_entry_per_engine_across_variants(monkeypatch):
    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            if kwargs["backend"] == "duckduckgo":
                return [{"title": "Hit", "href": "https://example.com/shared", "body": "Snippet"}]
            suffix = "quoted" if query.startswith('"') else "plain"
            return [
                {"title": "Hit", "href": f"https://example.com/brave-{suffix}", "body": "Snippet"}
            ]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    run = asyncio.run(
        Josty(backends=("duckduckgo,brave",)).search_run("alpha", mode="exact", limit=5)
    )
    by_provider = {status.provider: status for status in run.providers}
    assert len(run.providers) == 2
    assert by_provider["duckduckgo"].result_count == 1
    assert by_provider["brave"].result_count == 2
    assert all(status.ok for status in run.providers)
    assert all(status.error_kind is None for status in run.providers)


def test_aggregated_status_keeps_most_severe_variant_outcome(monkeypatch):
    class FlakyDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            if query.startswith('"'):
                raise RuntimeError("blocked")
            return [{"title": "Hit", "href": "https://example.com/a", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", FlakyDDGS)
    run = asyncio.run(
        Josty(backends=("duckduckgo",)).search_run("alpha", mode="exact", limit=5)
    )
    status = run.providers[0]
    assert status.provider == "duckduckgo"
    assert status.ok is True
    assert status.result_count == 1
    assert status.error_kind == "unknown"
    assert "RuntimeError: blocked" in status.error
    # Regression: a partially-failed engine (ok=true, failure error_kind) must
    # mark the run degraded — a variant did fail, so "complete" would be a lie.
    assert run.partial is True
    assert run.status == "degraded"


def test_aggregated_empty_variant_does_not_label_a_hit_empty(monkeypatch):
    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            if query.startswith('"'):
                return []
            return [{"title": "Hit", "href": "https://example.com/a", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    run = asyncio.run(
        Josty(backends=("yahoo",), enable_cache=False).search_run(
            "alpha", mode="exact", limit=5
        )
    )
    status = run.providers[0]
    assert status.ok is True
    assert status.result_count >= 1
    assert status.error_kind is None
    assert status.error is None
    assert run.partial is False
    assert run.status == "complete"


def test_aggregated_skip_variant_does_not_label_a_hit_skipped():
    hit = SearchResult("Hit", "https://example.com/a", "s", sources=["brave"])
    ok = ProviderStatus("brave", "q", True, 1, error_kind=None)
    skip = ProviderStatus(
        "brave",
        '"q"',
        False,
        0,
        error="skipped: engine in cool-down until 2099-01-01T00:00:00Z",
        error_kind="skipped",
    )
    agg = _aggregate_engine_status([ok, skip], [[hit], []])
    assert agg.ok is True
    assert agg.result_count == 1
    assert agg.error_kind is None
    assert agg.error is None


def test_aggregated_all_empty_stays_empty():
    empty = ProviderStatus("yahoo", "q", True, 0, error_kind="empty")
    agg = _aggregate_engine_status([empty, empty], [[], []])
    assert agg.ok is True
    assert agg.result_count == 0
    assert agg.error_kind == "empty"
    assert agg.error is None
    run = SearchRun("q", results=[], providers=[agg])
    assert run.partial is False
    assert run.status == "complete"


def test_duplicate_engine_across_groups_is_queried_once(monkeypatch):
    # Regression: an engine configured in multiple groups must be called and
    # reported exactly once (in its first group), not once per group.
    calls = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            calls.append(kwargs["backend"])
            return [{"title": "Hit", "href": "https://example.com/a", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)

    run = asyncio.run(Josty(backends=("brave", "brave")).search_run("q", limit=3))
    assert calls == ["brave"]
    assert [status.provider for status in run.providers] == ["brave"]

    calls.clear()
    run = asyncio.run(
        Josty(backends=("brave,duckduckgo", "brave")).search_run("q", limit=3)
    )
    assert sorted(calls) == ["brave", "duckduckgo"]
    assert sorted(status.provider for status in run.providers) == ["brave", "duckduckgo"]


def test_unavailable_engine_is_skipped_visibly_without_calling_ddgs(monkeypatch):
    class ExplodingDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise AssertionError("ddgs must not be called for unavailable engines")

    monkeypatch.setattr("josty.engine.DDGS", ExplodingDDGS)
    run = asyncio.run(Josty(backends=("nosuchengine",)).search_run("query", limit=5))
    assert run.results == []
    assert run.providers[0].provider == "nosuchengine"
    assert run.providers[0].ok is False
    assert run.providers[0].error_kind == "skipped"
    assert "not enabled in the installed ddgs" in run.providers[0].error
    assert run.status == "failed"


def test_merge_query_variants_best_rank_merge_without_frequency_vote():
    # Josty owns group-internal ordering: a URL's position is its best rank
    # across engine lists (no ddgs-style frequency-inflated ordering). RRF
    # voting is per ENGINE, not per list position: a URL found by both
    # engines of a group carries both engines' votes, per the per-engine
    # attribution contract (PROJECT.md "Transparent RRF Attribution
    # Contract"). This supersedes the earlier one-vote-per-group policy that
    # could not attribute contributions verifiably.
    from josty.engine import SearchResult, merge_query_variants, rrf

    def item(url, source):
        return SearchResult(source, url, "query", sources=[source])

    engine_a = [
        item("https://a.example/a", "a"),
        item("https://shared.example/x", "a"),
    ]
    engine_b = [
        item("https://b.example/b", "b"),
        item("https://shared.example/x", "b"),
    ]
    merged = merge_query_variants([engine_a, engine_b])
    assert [entry.url for entry in merged] == [
        "https://a.example/a",
        "https://b.example/b",
        "https://shared.example/x",
    ]
    fused = rrf([merged])
    scores = {entry.url: entry.score for entry in fused}
    # The shared URL carries both engines' votes at their discovery rank 2
    # each: 2 * round(1/62, 6) — one vote per engine, from engine_ranks.
    assert scores["https://shared.example/x"] == round(2 * round(1 / 62, 6), 6)
    shared = next(e for e in fused if e.url == "https://shared.example/x")
    assert shared.engine_ranks == {"a": 2, "b": 2}
    assert shared.rank_contributions == {"a": round(1 / 62, 6), "b": round(1 / 62, 6)}


def test_news_metadata_and_filters_are_preserved(monkeypatch):
    captured = {}

    class CapturingDDGS:
        def __init__(self, **kwargs):
            pass

        def news(self, query, **kwargs):
            captured.update(query=query, **kwargs)
            return [
                {
                    "title": "News",
                    "url": "https://example.com/news",
                    "body": "Snippet",
                    "date": "2026-07-15",
                    "source": "Example News",
                }
            ]

    monkeypatch.setattr("josty.engine.DDGS", CapturingDDGS)
    run = asyncio.run(
        Josty(backends=("duckduckgo",)).search_run(
            "query",
            limit=3,
            category="news",
            region="de-de",
            safesearch="on",
            timelimit="w",
        )
    )
    item = run.results[0]
    assert (item.published_at, item.publisher) == ("2026-07-15", "Example News")
    assert captured == {
        "query": "query",
        "backend": "duckduckgo",
        "max_results": 3,
        "safesearch": "on",
        "region": "de-de",
        "timelimit": "w",
    }


def test_github_uses_best_match_and_normalizes_results(monkeypatch):
    captured = {}

    async def fake_get(self, url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "items": [
                    {
                        "full_name": "owner/repo",
                        "html_url": "https://github.com/owner/repo",
                        "description": "A repo",
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    results, status = asyncio.run(Josty().github_run("agent search", 5))
    assert status.ok
    assert results[0].sources == ["github-api"]
    assert captured["params"] == {"q": "agent search", "per_page": 5}


def test_github_is_opt_in_and_fused_once(monkeypatch):
    engine = Josty()
    a1 = result("https://example.com/a", source="one")
    a2 = result("https://example.com/a", source="two")
    repo = result("https://github.com/owner/repo", source="github-api")

    async def parts(*args, **kwargs):
        return [[a1], [a2]], [ProviderStatus("web", "q", True, 1)], []

    async def github(*args, **kwargs):
        return [repo], ProviderStatus("github-api", "q", True, 1)

    monkeypatch.setattr(engine, "_search_parts", parts)
    monkeypatch.setattr(engine, "github_run", github)

    web_only = asyncio.run(engine.research_run("q"))
    assert [item.url for item in web_only.results] == ["https://example.com/a"]

    combined = asyncio.run(engine.research_run("q", include_github=True))
    assert combined.results[0].url == "https://example.com/a"
    assert combined.results[0].sources == ["one", "two"]
    # Per-engine attribution contract: each engine contributes
    # round(1/(k+rank), 6) and the score derives from those rounded terms
    # (see PROJECT.md "Transparent RRF Attribution Contract").
    assert combined.results[0].score == round(2 * round(1 / 61, 6), 6)
    assert combined.results[1].score == round(1.2 * round(1 / 61, 6), 6)


def test_diagnose_probes_each_backend_group_host_with_bare_get(monkeypatch):
    seen = []

    async def fake_get(self, url, **kwargs):
        seen.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(Josty().diagnose_run()).dict()

    expected = {
        Josty.BACKEND_HOSTS[name]
        for group in Josty.DEFAULT_BACKENDS
        for name in group.split(",")
    }
    seen_hosts = {u for u in seen}
    assert seen_hosts == {f"https://{host}/" for host in expected}
    assert all(url.startswith("https://") for url in seen)

    by_host = {entry["host"]: entry for entry in payload["providers"]}
    assert set(by_host) == expected
    assert all(entry["ok"] and entry["http_status"] == 200 for entry in by_host.values())
    assert payload["status"] == "complete"
    assert payload["schema_version"] == "1.0"


def test_diagnose_reports_http_status_for_challenged_hosts(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(403, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(Josty().diagnose_run()).dict()
    entry = next(item for item in payload["providers"] if item["provider"] == "duckduckgo")
    assert entry["ok"] is True
    assert entry["http_status"] == 403
    assert entry["challenged"] is True


def test_diagnose_skips_open_breaker_without_network(monkeypatch):
    hits = {"n": 0}

    async def fake_get(self, url, **kwargs):
        hits["n"] += 1
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    breaker = CircuitBreaker(fail_threshold=1, window_seconds=60, cool_down_seconds=30)
    breaker.record_failure("brave", "search")
    engine = Josty(backends=("brave",), breaker=breaker)
    payload = asyncio.run(engine.diagnose_run()).dict()
    entry = next(item for item in payload["providers"] if item["provider"] == "brave")
    assert hits["n"] == 0
    assert entry["ok"] is False
    assert entry["error_kind"] == "skipped"
    assert entry["circuit_state"] == "open"
    assert entry["error"] is not None
    assert entry["error"].startswith("skipped: engine in cool-down until ")


def test_diagnose_marks_429_as_challenged(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(Josty().diagnose_run()).dict()
    entry = next(item for item in payload["providers"] if item["provider"] == "brave")
    assert entry["ok"] is True
    assert entry["http_status"] == 429
    assert entry["challenged"] is True


def test_diagnose_200_is_not_challenged(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(Josty().diagnose_run()).dict()
    entry = next(item for item in payload["providers"] if item["provider"] == "duckduckgo")
    assert entry["ok"] is True
    assert entry["challenged"] is False


def test_diagnose_classifies_blocked_hosts(monkeypatch):
    hosts_to_exc = {
        "www.google.com": httpx.ReadTimeout("timed out"),
        "search.yahoo.com": httpx.ConnectError("refused", request=httpx.Request("GET", "u")),
        "www.mojeek.com": _dns_connect_error(),
        "www.startpage.com": RuntimeError("boom"),
        "search.brave.com": _tls_connect_error(),
    }

    def exc_for(url):
        return hosts_to_exc.get(urlsplit(url).hostname)

    async def fake_get(self, url, **kwargs):
        exc = exc_for(url)
        if exc is not None:
            raise exc
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(
        Josty(backends=("google,yahoo,mojeek,startpage,brave",)).diagnose_run()
    ).dict()

    by_provider = {entry["provider"]: entry for entry in payload["providers"]}
    assert by_provider["google"]["error_kind"] == "timeout"
    assert by_provider["yahoo"]["error_kind"] == "network"
    assert by_provider["mojeek"]["error_kind"] == "dns"
    assert by_provider["brave"]["error_kind"] == "tls"
    assert by_provider["startpage"]["error_kind"] == "unknown"
    for provider in ("google", "yahoo", "mojeek", "brave", "startpage"):
        entry = by_provider[provider]
        assert entry["ok"] is False
        assert entry["http_status"] is None
        assert entry["error"]
        assert entry["challenged"] is False
    assert payload["status"] == "failed"


def test_diagnose_reports_unavailable_engine_as_skipped_without_probing(monkeypatch):
    async def fail_get(self, url, **kwargs):
        raise AssertionError("no probe should run")

    monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)
    payload = asyncio.run(Josty(backends=("mystery",)).diagnose_run()).dict()
    by_provider = {entry["provider"]: entry for entry in payload["providers"]}
    assert set(by_provider) == {"mystery"}
    assert by_provider["mystery"]["ok"] is False
    assert by_provider["mystery"]["error_kind"] == "skipped"
    assert "not enabled in the installed ddgs" in by_provider["mystery"]["error"]
    assert payload["status"] == "failed"


def test_diagnose_probes_grokipedia_and_wikipedia_hosts(monkeypatch):
    grok_ok, _ = _engine_available("text", "grokipedia")
    wiki_ok, _ = _engine_available("text", "wikipedia")
    if not grok_ok or not wiki_ok:
        pytest.skip("grokipedia/wikipedia not enabled in installed ddgs")

    seen: list[str] = []

    async def fake_get(self, url, **kwargs):
        seen.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(
        Josty(backends=("grokipedia,wikipedia",)).diagnose_run()
    ).dict()
    by_provider = {entry["provider"]: entry for entry in payload["providers"]}
    assert by_provider["grokipedia"]["ok"] is True
    assert by_provider["grokipedia"]["host"] == "grokipedia.com"
    assert by_provider["wikipedia"]["ok"] is True
    assert by_provider["wikipedia"]["host"] == "en.wikipedia.org"
    assert by_provider["grokipedia"]["error_kind"] is None
    assert any(urlsplit(url).hostname == "grokipedia.com" for url in seen)
    assert any(urlsplit(url).hostname == "en.wikipedia.org" for url in seen)


def test_diagnose_unmapped_available_engine_is_named_skip(monkeypatch):
    grok_ok, _ = _engine_available("text", "grokipedia")
    if not grok_ok:
        pytest.skip("grokipedia not enabled in installed ddgs")

    hosts = {key: value for key, value in Josty.BACKEND_HOSTS.items() if key != "grokipedia"}
    monkeypatch.setattr(Josty, "BACKEND_HOSTS", hosts)

    async def fail_get(self, url, **kwargs):
        raise AssertionError(f"must not probe {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)
    payload = asyncio.run(Josty(backends=("grokipedia",)).diagnose_run()).dict()
    entry = payload["providers"][0]
    assert entry["provider"] == "grokipedia"
    assert entry["ok"] is False
    assert entry["error_kind"] == "skipped"
    assert "no known upstream host" in entry["error"]


def test_diagnose_scopes_probes_to_category_and_github_opt_in(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    text = asyncio.run(Josty().diagnose_run()).dict()
    text_providers = {entry["provider"] for entry in text["providers"]}
    assert "github-api" not in text_providers
    assert all(p in text_providers for p in ("brave", "google", "yahoo"))

    news = asyncio.run(Josty().diagnose_run(category="news")).dict()
    news_providers = {entry["provider"] for entry in news["providers"]}
    assert news_providers == {"bing", "duckduckgo", "yahoo"}

    got = asyncio.run(Josty().diagnose_run(include_github=True)).dict()
    got_providers = {entry["provider"] for entry in got["providers"]}
    assert "github-api" in got_providers


def test_domain_weights_boost_and_penalize_with_subdomains_and_profiles():
    # General profile (default)
    assert domain_weight("https://docs.python.org/3/") == 1.2
    assert domain_weight("https://example.readthedocs.io/en/latest/") == 1.2
    assert domain_weight("https://github.com/astral-sh/ruff") == 1.2
    assert domain_weight("https://api.github.com/repos/astral-sh/ruff") == 1.2
    assert domain_weight("https://stackoverflow.com/questions/123") == 1.2
    assert domain_weight("https://pinterest.com/pin/123") == 0.6
    assert domain_weight("https://quora.com/topic") == 0.6
    assert domain_weight("https://example.com/blog/article") == 1.0

    # Dev profile
    assert domain_weight("https://github.com/python/cpython", profile="dev") == 1.3
    assert domain_weight("https://api.github.com/repos", profile="dev") == 1.3
    assert domain_weight("https://react.dev/reference/react", profile="dev") == 1.3
    assert domain_weight("https://pkg.go.dev/net/http", profile="dev") == 1.3
    assert domain_weight("https://npmjs.com/package/express", profile="dev") == 1.3
    assert domain_weight("https://crates.io/crates/tokio", profile="dev") == 1.3
    assert domain_weight("https://pinterest.com/pin/123", profile="dev") == 0.5
    assert domain_weight("https://geeksforgeeks.org/python", profile="dev") == 0.5

    # Academic profile
    assert domain_weight("https://arxiv.org/abs/2301.00001", profile="academic") == 1.4
    assert domain_weight("https://pubmed.ncbi.nlm.nih.gov/12345678/", profile="academic") == 1.4
    assert domain_weight("https://ieeexplore.ieee.org/document/12345", profile="academic") == 1.4
    assert domain_weight("https://dl.acm.org/doi/10.1145/123", profile="academic") == 1.4
    assert domain_weight("https://nature.com/articles/s41586-023", profile="academic") == 1.4
    # Documentation and encyclopedia retain authoritative baseline in academic mode
    assert domain_weight("https://docs.python.org/3/", profile="academic") == 1.2
    assert domain_weight("https://en.wikipedia.org/wiki/Search_engine", profile="academic") == 1.2
    assert domain_weight("https://pinterest.com/pin/123", profile="academic") == 0.5


def test_invalid_profile_raises_value_error():
    with pytest.raises(ValueError, match="profile"):
        Josty(profile="unsupported")

    engine = Josty()
    with pytest.raises(ValueError, match="profile"):
        asyncio.run(engine.search_run("query", profile="unsupported"))

    with pytest.raises(ValueError, match="profile"):
        asyncio.run(engine.research_run("query", profile="unsupported"))


def test_cache_keys_are_isolated_by_profile():
    general_key = SearchCache.hash_key("query", profile="general")
    dev_key = SearchCache.hash_key("query", profile="dev")
    academic_key = SearchCache.hash_key("query", profile="academic")

    assert general_key != dev_key
    assert dev_key != academic_key
    assert general_key != academic_key


def test_search_cache_hit_and_miss_and_clear(tmp_path):
    cache_file = tmp_path / "test_cache.db"
    cache = SearchCache(cache_file, default_ttl=3600)
    key = SearchCache.hash_key("test query", limit=5)

    assert cache.get(key) is None

    payload = {"query": "test query", "status": "complete", "results": []}
    cache.set(key, payload)
    assert cache.get(key) == payload

    cache.clear()
    assert cache.get(key) is None


def test_cache_stats_starts_and_resets_empty(tmp_path):
    cache = SearchCache(tmp_path / "stats.db", default_ttl=60.0)
    assert cache.stats() == {"rows": 0, "bytes": 0, "hits": 0}
    key = SearchCache.hash_key("query")
    cache.set(key, {"query": "query"})
    cache.get(key)
    cache.clear()
    assert cache.stats() == {"rows": 0, "bytes": 0, "hits": 0}


def test_search_cache_increments_hit_count(tmp_path):
    cache = SearchCache(tmp_path / "hits.db", default_ttl=60.0)
    key = SearchCache.hash_key("query")
    cache.set(key, {"query": "query"})
    stats = cache.stats()
    assert stats["rows"] == 1
    assert stats["hits"] == 0
    cache.get(key)
    cache.get(key)
    with sqlite3.connect(cache.db_path) as conn:
        hits, last_accessed = conn.execute(
            "SELECT hit_count, last_accessed FROM search_cache WHERE key = ?", (key,)
        ).fetchone()
    assert hits == 2
    assert last_accessed > 0


def test_search_cache_prunes_when_over_max_rows(tmp_path):
    cache = SearchCache(tmp_path / "prune.db", default_ttl=60.0, max_rows=5, prune_batch=2)
    for index in range(6):
        cache.set(f"k{index}", {"n": index})
    assert cache.stats()["rows"] == 5


def test_search_cache_prune_does_not_wipe_table(tmp_path):
    cache = SearchCache(tmp_path / "wipe.db", default_ttl=60.0, max_rows=3, prune_batch=100)
    for index in range(4):
        cache.set(f"k{index}", {"n": index})
    assert cache.stats()["rows"] == 3


def test_cache_prunes_when_over_max_bytes(tmp_path):
    cache = SearchCache(
        tmp_path / "bytes.db", default_ttl=60.0, max_bytes=2000, prune_batch=1
    )
    big = "x" * 1000
    for index in range(3):
        cache.set(f"k{index}", {"blob": big})
    stats = cache.stats()
    assert stats["bytes"] <= 2000
    assert stats["rows"] < 3


def test_max_bytes_disabled_when_zero(tmp_path):
    cache = SearchCache(tmp_path / "nobytes.db", default_ttl=60.0, max_bytes=0)
    for index in range(5):
        cache.set(f"k{index}", {"blob": "x" * 1000})
    stats = cache.stats()
    assert stats["rows"] == 5
    assert stats["bytes"] >= 5000


def test_search_cache_migrates_legacy_schema(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE search_cache (
                key TEXT PRIMARY KEY,
                created_at REAL,
                expires_at REAL,
                payload TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO search_cache VALUES (?, ?, ?, ?)",
            ("k", 1.0, 9_999_999_999.0, '{"ok": true}'),
        )
    cache = SearchCache(db, default_ttl=60.0)
    assert cache.get("k") == {"ok": True}
    with sqlite3.connect(db) as conn:
        hits, last_accessed = conn.execute(
            "SELECT hit_count, last_accessed FROM search_cache WHERE key = 'k'"
        ).fetchone()
    assert hits == 1
    assert last_accessed > 0


def test_search_cache_ttl_expiration(tmp_path):
    cache_file = tmp_path / "test_cache_ttl.db"
    cache = SearchCache(cache_file, default_ttl=0.01)
    key = SearchCache.hash_key("query")
    cache.set(key, {"cached": True}, ttl=-1.0)
    assert cache.get(key) is None


def test_cache_does_not_persist_content(tmp_path, monkeypatch):
    import json as _json

    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            return [{"title": "Hit", "href": "https://example.com/doc", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    engine = Josty(cache_db=tmp_path / "c.db", max_content_chars=8000)

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)
    downloads = {"n": 0}

    async def fake_download(_self, client, url):
        downloads["n"] += 1
        return f"<html><body><p>{'a' * 9000}</p></body></html>", url

    monkeypatch.setattr(Josty, "_download", fake_download)

    first = asyncio.run(engine.search_run("fetch query", limit=1, fetch=True))
    assert first.results[0].content is not None
    assert downloads["n"] == 1

    with sqlite3.connect(str(engine.cache.db_path)) as conn:
        rows = conn.execute("SELECT payload FROM search_cache").fetchall()
    assert len(rows) == 1
    cached_payload = _json.loads(rows[0][0])
    assert cached_payload["results"][0]["content"] is None
    assert cached_payload["results"][0]["fetched_at"] is None
    assert len(rows[0][0]) < 5000

    second = asyncio.run(engine.search_run("fetch query", limit=1, fetch=True))
    assert second.cached is True
    assert downloads["n"] == 2
    assert second.results[0].content is not None
    assert len(second.results[0].content) == 8000


def test_fetch_false_serp_hit_has_no_content_and_no_download(tmp_path, monkeypatch):
    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            return [{"title": "Hit", "href": "https://example.com/doc", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    engine = Josty(cache_db=tmp_path / "c.db")

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)
    downloads = {"n": 0}

    async def fake_download(_self, client, url):
        downloads["n"] += 1
        return "<html><body><p>text</p></body></html>", url

    monkeypatch.setattr(Josty, "_download", fake_download)

    asyncio.run(engine.search_run("serp query", limit=1))
    assert downloads["n"] == 0  # fetch=False never downloads
    hit = asyncio.run(engine.search_run("serp query", limit=1))
    assert hit.cached is True
    assert downloads["n"] == 0
    assert hit.results[0].content is None


def test_old_payload_with_content_still_loads(tmp_path, monkeypatch):
    engine = Josty(cache_db=tmp_path / "c.db")
    legacy = {
        "schema_version": "1.0",
        "query": "legacy",
        "status": "complete",
        "count": 1,
        "partial": False,
        "cached": False,
        "providers": [{"provider": "b", "query": "legacy", "ok": True, "result_count": 1}],
        "results": [
            {
                "title": "t",
                "url": "https://example.com/x",
                "snippet": "",
                "sources": ["b"],
                "score": 0.0,
                "content": "old cached body",
                "extraction_method": "trafilatura",
                "fetched_url": "https://example.com/x",
                "fetched_at": "2026-01-01T00:00:00+00:00",
                "fetch_error": None,
            }
        ],
    }
    key = SearchCache.hash_key("legacy")
    engine.cache.set(key, legacy)
    restored = engine.cache.get(key)
    run = _search_run_from_dict(restored)
    assert run.results[0].content == "old cached body"


def test_search_run_uses_cache_and_skips_network(tmp_path, monkeypatch):
    cache_file = tmp_path / "engine_cache.db"
    engine = Josty(cache_db=cache_file)

    calls = 0

    class CountingDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return [{"title": "Hit", "href": "https://example.com/doc", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", CountingDDGS)

    # First call - populates cache
    first_run = asyncio.run(engine.search_run("cached query", limit=1))
    assert calls > 0
    assert len(first_run.results) == 1
    assert first_run.cached is False

    recorded_calls = calls

    # Second call - returns from cache with 0 additional network calls
    second_run = asyncio.run(engine.search_run("cached query", limit=1))
    assert calls == recorded_calls
    assert len(second_run.results) == 1
    assert second_run.results[0].title == "Hit"
    assert second_run.cached is True


def test_fetch_content_extracts_markdown_and_captures_error(monkeypatch):
    engine = Josty()
    item_ok = result("https://example.com/ok")
    item_fail = result("https://example.com/fail")

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def fake_download(_self, client, url):
        if "fail" in url:
            raise ValueError("connection dropped")
        return "<html><body><h1>Doc</h1><p>Text</p></body></html>", url

    monkeypatch.setattr(Josty, "_download", fake_download)

    asyncio.run(engine.fetch_content([item_ok, item_fail]))
    assert item_ok.content is not None
    assert item_ok.extraction_method in ("trafilatura", "html-text-fallback")
    assert item_ok.fetched_at is not None
    assert item_fail.content is None
    assert "connection dropped" in (item_fail.fetch_error or "")


def test_fetch_content_concurrent_thread_safety(monkeypatch):
    engine = Josty(max_fetch_concurrency=10)
    items = [result(f"https://example.com/page{i}") for i in range(20)]

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def fake_download(_self, client, url):
        html = f"<html><body><h1>Title for {url}</h1><p>Body paragraph content</p></body></html>"
        return html, url

    monkeypatch.setattr(Josty, "_download", fake_download)

    asyncio.run(engine.fetch_content(items))
    assert len(items) == 20
    for item in items:
        assert item.content is not None
        assert item.extraction_method in ("trafilatura", "html-text-fallback")
        assert item.fetched_at is not None
        assert item.fetch_error is None


def test_expand_with_max_query_variants_truncation_and_validation():
    # Validation
    with pytest.raises(ValueError, match="max_query_variants must be positive"):
        Josty.expand("test", max_query_variants=0)
    with pytest.raises(ValueError, match="max_query_variants must be positive"):
        Josty.expand("test", max_query_variants=-1)

    # OSS mode generates 4 variants by default; cap at 2
    uncapped = Josty.expand("test query", mode="oss")
    assert len(uncapped) == 4
    capped = Josty.expand("test query", mode="oss", max_query_variants=2)
    assert capped == uncapped[:2]
    assert len(capped) == 2

    # Multi-site exact generates 4 variants (2 sites x 2 variants); cap at 3
    multi_site = Josty.expand(
        "test query", sites=["github.com", "gitlab.com"], mode="exact"
    )
    assert len(multi_site) == 4
    capped_multi = Josty.expand(
        "test query",
        sites=["github.com", "gitlab.com"],
        mode="exact",
        max_query_variants=3,
    )
    assert capped_multi == multi_site[:3]
    assert len(capped_multi) == 3


def test_constructor_max_query_variants_validation():
    with pytest.raises(ValueError, match="max_query_variants must be positive"):
        Josty(max_query_variants=0)
    with pytest.raises(ValueError, match="max_query_variants must be positive"):
        Josty(max_query_variants=-5)

    engine = Josty(max_query_variants=2)
    assert engine.max_query_variants == 2


def test_search_run_honors_max_query_variants_and_isolates_cache(monkeypatch):
    queries_seen = []

    class MockDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            queries_seen.append(query)
            return [{"title": "Title", "href": "https://github.com/doc", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", MockDDGS)

    engine = Josty(backends=("duckduckgo",), enable_cache=True)

    # Mode oss with 2 sites creates 8 variants uncapped; cap at 2
    queries_seen.clear()
    run1 = asyncio.run(
        engine.search_run(
            "my query",
            sites=["github.com", "gitlab.com"],
            mode="oss",
            max_query_variants=2,
        )
    )
    assert len(queries_seen) == 2
    assert len(run1.results) == 1

    # Same query with max_query_variants=3 should be a cache miss and execute 3 queries
    queries_seen.clear()
    run2 = asyncio.run(
        engine.search_run(
            "my query",
            sites=["github.com", "gitlab.com"],
            mode="oss",
            max_query_variants=3,
        )
    )
    assert len(queries_seen) == 3
    assert len(run2.results) == 1


def test_fetch_content_browser_headers_and_truncation(monkeypatch):
    from josty.engine import BROWSER_FETCH_HEADERS

    captured_headers = {}

    async def allow(_self, _url):
        return None

    monkeypatch.setattr(Josty, "_validate_public_url", allow)

    async def fake_download(_self, client, url):
        captured_headers.update(dict(client.headers))
        long_html = f"<html><body><p>{'a' * 5000}</p></body></html>"
        return long_html, url

    monkeypatch.setattr(Josty, "_download", fake_download)

    # 1. Test truncation at max_content_chars = 1000
    engine_capped = Josty(max_content_chars=1000)
    item1 = result("https://example.com/test1")
    asyncio.run(engine_capped.fetch_content([item1]))
    assert item1.content is not None
    assert len(item1.content) == 1000
    assert captured_headers.get("user-agent") == BROWSER_FETCH_HEADERS["User-Agent"]
    assert captured_headers.get("sec-ch-ua") == BROWSER_FETCH_HEADERS["sec-ch-ua"]
    assert captured_headers.get("sec-ch-ua-platform") == BROWSER_FETCH_HEADERS["sec-ch-ua-platform"]

    # 2. Test unlimited when max_content_chars = 0
    engine_unlimited = Josty(max_content_chars=0)
    item2 = result("https://example.com/test2")
    asyncio.run(engine_unlimited.fetch_content([item2]))
    assert item2.content is not None
    assert len(item2.content) > 1000


def test_max_content_chars_validation():
    with pytest.raises(ValueError, match="content limits must be positive"):
        Josty(max_content_chars=-1)

    with pytest.raises(ValueError, match="content limits must be positive"):
        Josty(max_download_bytes=0)


def test_domain_weights_expanded_authoritative_sets():
    from josty.engine import domain_weight

    # Dev profile boosts AI & modern dev domains
    assert domain_weight("https://huggingface.co/models", profile="dev") == 1.3
    assert domain_weight("https://docs.vllm.ai/quickstart", profile="dev") == 1.3
    assert domain_weight("https://astral.sh/blog", profile="dev") == 1.3
    assert domain_weight("https://ollama.com/library", profile="dev") == 1.3

    # Academic profile boosts ML conference & preprint domains
    assert domain_weight("https://openreview.net/forum?id=123", profile="academic") == 1.4
    assert domain_weight("https://paperswithcode.com/sota", profile="academic") == 1.4
    assert domain_weight("https://neurips.cc/virtual/2024", profile="academic") == 1.4

    # Spam domains are still penalized
    assert domain_weight("https://geeksforgeeks.org/article", profile="dev") == 0.5

def test_search_run_and_research_run_behavior_parity(tmp_path, monkeypatch):
    engine = Josty(enable_cache=False)

    async def fake_search_parts(query, **kwargs):
        res = [SearchResult(title=f"Result for {query}", url="https://example.com/res")]
        status = [ProviderStatus("bing", query, True, 1)]
        return [res], status, []

    monkeypatch.setattr(engine, "_search_parts", fake_search_parts)

    search_res = asyncio.run(engine.search_run("test parity query", limit=5, mode="exact"))
    research_res = asyncio.run(
        engine.research_run("test parity query", limit=5, mode="exact", include_github=False)
    )

    search_dict = search_res.dict()
    research_dict = research_res.dict()
    search_dict.pop("run_at", None)
    research_dict.pop("run_at", None)
    assert search_dict == research_dict
    assert search_res.cached is False
    assert search_res.status == "complete"
    assert len(search_res.results) == 1
    assert search_res.results[0].title == "Result for test parity query"


def test_circuit_breaker_opens_after_threshold_failures_and_skips_calls():
    breaker = CircuitBreaker(fail_threshold=3, window_seconds=60, cool_down_seconds=30)
    for _ in range(3):
        breaker.record_failure("bing", "search")
    allowed, message = breaker.status("bing", "search")
    assert allowed is False
    assert message is not None
    assert message.startswith("skipped: engine in cool-down until ")
    assert message.endswith("Z")


def test_circuit_breaker_is_per_backend_and_per_error_class():
    breaker = CircuitBreaker(fail_threshold=2, window_seconds=60, cool_down_seconds=30)
    breaker.record_failure("bing", "search")
    breaker.record_failure("bing", "search")
    assert breaker.status("bing", "search")[0] is False
    assert breaker.status("brave", "search")[0] is True
    assert breaker.status("bing", "fetch")[0] is True


def test_circuit_breaker_recovers_after_cool_down(monkeypatch):
    breaker = CircuitBreaker(fail_threshold=2, window_seconds=60, cool_down_seconds=30)
    monkeypatch.setattr("josty.engine.time.monotonic", lambda: 1000.0)
    breaker.record_failure("bing", "search")
    breaker.record_failure("bing", "search")
    assert breaker.status("bing", "search")[0] is False
    monkeypatch.setattr("josty.engine.time.monotonic", lambda: 2000.0)
    allowed, message = breaker.status("bing", "search")
    assert allowed is True
    assert message is None


def test_circuit_breaker_does_not_extend_cool_down_on_repeated_failures(monkeypatch):
    breaker = CircuitBreaker(fail_threshold=3, window_seconds=60, cool_down_seconds=30)
    # Freeze wall clock too so the iso timestamp doesn't drift
    monkeypatch.setattr("josty.engine.time.time", lambda: 1_700_000_000.0)
    monkeypatch.setattr("josty.engine.time.monotonic", lambda: 1000.0)
    for _ in range(3):
        breaker.record_failure("bing", "search")
    _, first_msg = breaker.status("bing", "search")
    assert first_msg is not None
    first_until = first_msg.split("until ")[1]
    # 4th through 10th failures during cool-down must NOT extend the timer
    for _ in range(7):
        breaker.record_failure("bing", "search")
    _, later_msg = breaker.status("bing", "search")
    later_until = later_msg.split("until ")[1]
    assert first_until == later_until, "cool-down timer must freeze at the trip point"


def test_empty_variants_do_not_clear_rate_limit_failures(monkeypatch):
    from ddgs.exceptions import DDGSException, RatelimitException

    n = {"i": 0}

    class FlipDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            n["i"] += 1
            if n["i"] % 2 == 1:
                raise RatelimitException("429")
            raise DDGSException("No results found")

    monkeypatch.setattr("josty.engine.DDGS", FlipDDGS)
    breaker = CircuitBreaker(fail_threshold=2, window_seconds=60, cool_down_seconds=30)
    engine = Josty(backends=("duckduckgo",), enable_cache=False, breaker=breaker)
    asyncio.run(engine.search_run("document indexing", mode="oss", limit=5))
    allowed, message = breaker.status("duckduckgo", "search")
    assert allowed is False
    assert message is not None
    assert "cool-down" in message


def test_empty_only_sequence_does_not_open_breaker(monkeypatch):
    from ddgs.exceptions import DDGSException

    class EmptyDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise DDGSException("no results found")

    monkeypatch.setattr("josty.engine.DDGS", EmptyDDGS)
    breaker = CircuitBreaker(fail_threshold=3, window_seconds=60, cool_down_seconds=30)
    engine = Josty(backends=("duckduckgo",), enable_cache=False, breaker=breaker)
    for _ in range(4):
        run = asyncio.run(engine.search_run("anything", limit=5))
        assert run.providers[0].error_kind == "empty"
        assert run.status == "complete"
    assert breaker.status("duckduckgo", "search")[0] is True


def test_circuit_breaker_thread_hammer_does_not_raise():
    import threading
    import time

    breaker = CircuitBreaker(fail_threshold=1, window_seconds=1, cool_down_seconds=0.01)
    errors: list[Exception] = []
    stop = threading.Event()

    def hammer_status() -> None:
        while not stop.is_set():
            try:
                breaker.status("brave", "search")
            except Exception as exc:
                errors.append(exc)

    def hammer_mutate() -> None:
        while not stop.is_set():
            try:
                breaker.record_failure("brave", "search")
                breaker.record_success("brave", "search")
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=hammer_status) for _ in range(4)]
    threads.append(threading.Thread(target=hammer_mutate))
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    stop.set()
    for thread in threads:
        thread.join()
    assert errors == []


def test_circuit_breaker_success_clears_history():
    breaker = CircuitBreaker(fail_threshold=3, window_seconds=60, cool_down_seconds=30)
    breaker.record_failure("bing", "search")
    breaker.record_failure("bing", "search")
    breaker.record_success("bing", "search")
    breaker.record_failure("bing", "search")
    assert breaker.status("bing", "search")[0] is True


def test_circuit_breaker_validates_constructor():
    with pytest.raises(ValueError):
        CircuitBreaker(fail_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(window_seconds=0)
    with pytest.raises(ValueError):
        CircuitBreaker(cool_down_seconds=0)


def test_search_run_skips_backend_in_cool_down(monkeypatch):
    class BrokenDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise RuntimeError("blocked")

    monkeypatch.setattr("josty.engine.DDGS", BrokenDDGS)
    breaker = CircuitBreaker(fail_threshold=2, window_seconds=60, cool_down_seconds=30)
    engine = Josty(backends=("duckduckgo",), breaker=breaker)
    for _ in range(2):
        run = asyncio.run(engine.search_run("q", limit=3))
        assert run.providers[0].error == "RuntimeError: blocked"
    skipped = asyncio.run(engine.search_run("q", limit=3))
    assert skipped.providers[0].error is not None
    assert skipped.providers[0].error.startswith("skipped: engine in cool-down until ")
    assert skipped.providers[0].error.endswith("Z")
    assert skipped.results == []


def test_github_breaker_is_independent_from_search_backends(monkeypatch):
    class BrokenDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise RuntimeError("blocked")

    monkeypatch.setattr("josty.engine.DDGS", BrokenDDGS)
    breaker = CircuitBreaker(fail_threshold=2, window_seconds=60, cool_down_seconds=30)
    engine = Josty(backends=("duckduckgo",), breaker=breaker)

    async def fake_get(self, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"items": []},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    for _ in range(2):
        asyncio.run(engine.search_run("q", limit=3))
    assert breaker.status("duckduckgo", "search")[0] is False
    assert breaker.status("github-api", "search")[0] is True
    results, status = asyncio.run(engine.github_run("agent search", 5))
    assert status.ok is True
    assert results == []


def test_search_run_dict_includes_run_at():
    engine = Josty(backends=("test",), enable_cache=False)

    async def parts(query, **kwargs):
        return [[result("https://example.com/x")]], [ProviderStatus("test", query, True, 1)], []

    engine._search_parts = parts
    run = asyncio.run(engine.search_run("q", limit=1))
    payload = run.dict()
    assert isinstance(payload.get("run_at"), str)
    assert payload["run_at"].endswith("+00:00")


def test_cached_hit_preserves_run_at(tmp_path, monkeypatch):
    engine = Josty(cache_db=tmp_path / "c.db")
    calls = {"n": 0}

    class CountingDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            calls["n"] += 1
            return [{"title": "Hit", "href": "https://example.com/doc", "body": "Snippet"}]

    monkeypatch.setattr("josty.engine.DDGS", CountingDDGS)
    first = asyncio.run(engine.search_run("age query", limit=1))
    network_calls = calls["n"]
    assert network_calls > 0
    second = asyncio.run(engine.search_run("age query", limit=1))
    assert second.cached is True
    assert calls["n"] == network_calls
    assert second.run_at == first.run_at


def test_old_payload_without_run_at_loads():
    restored = _search_run_from_dict({"query": "q", "results": [], "providers": []})
    assert restored.run_at is None
    assert "run_at" not in restored.dict()


def test_ttl_floors_shorten_freshness_sensitive_searches():
    default = 21600.0
    assert _ttl_for("text", None, default) == 21600.0
    assert _ttl_for("text", "m", default) == 21600.0
    assert _ttl_for("text", "y", default) == 21600.0
    assert _ttl_for("news", None, default) == 3600.0
    assert _ttl_for("news", "m", default) == 3600.0
    assert _ttl_for("text", "w", default) == 7200.0
    assert _ttl_for("news", "d", default) == 1800.0
    assert _ttl_for("text", "d", default) == 1800.0
    # A caller-configured shorter TTL is always respected
    assert _ttl_for("text", None, 600.0) == 600.0
    assert _ttl_for("news", "d", 600.0) == 600.0


def test_news_day_timelimit_cache_row_uses_floor_ttl(tmp_path, monkeypatch):
    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            return [{"title": "Text", "href": "https://example.com/t", "body": "b"}]

        def news(self, *args, **kwargs):
            return [{"title": "News", "url": "https://example.com/n", "body": "b"}]

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    engine = Josty(cache_db=tmp_path / "c.db", cache_ttl=21600.0)
    asyncio.run(engine.search_run("today news", limit=1, category="news", timelimit="d"))
    asyncio.run(engine.search_run("plain query", limit=1))

    with sqlite3.connect(str(engine.cache.db_path)) as conn:
        rows = conn.execute(
            "SELECT payload, expires_at - created_at FROM search_cache"
        ).fetchall()
    # expires_at - created_at is a float subtraction of two timestamps; compare
    # with a 1s tolerance rather than ==, which can flake on rounding.
    assert len(rows) == 2
    assert any(abs(row[1] - 1800.0) < 1.0 for row in rows)
    assert any(abs(row[1] - 21600.0) < 1.0 for row in rows)
