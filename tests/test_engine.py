import asyncio
import socket
import ssl
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from deep_search.engine import (
    DeepSearch,
    ProviderStatus,
    SearchResult,
    SearchRun,
    canonical,
    merge_query_variants,
    normalize_sites,
    rrf,
)


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
    assert results[0].score == round(2 / 61, 6)
    assert first[0].score == 0.0  # fusion does not mutate caller-owned results


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
    assert DeepSearch.expand("company revenue history") == ["company revenue history"]
    assert DeepSearch.expand("agent search", mode="exact") == [
        "agent search",
        '"agent search"',
    ]
    assert DeepSearch.expand("agent search", mode="oss") == [
        "agent search",
        '"agent search"',
        "agent search open source",
        "agent search self-hosted",
    ]


def test_site_filters_scope_every_variant():
    assert DeepSearch.expand("agent search", ["GitHub.com"], mode="exact") == [
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

    monkeypatch.setattr("deep_search.engine.DDGS", FakeDDGS)
    run = asyncio.run(
        DeepSearch(backends=("test",)).search_run("query", sites=["github.com"], limit=3)
    )
    assert [item.url for item in run.results] == ["https://docs.github.com/a"]


def test_invalid_search_options_and_content_bounds_are_rejected():
    with pytest.raises(ValueError):
        asyncio.run(DeepSearch().search_run("query", limit=101))
    with pytest.raises(ValueError, match="category"):
        asyncio.run(DeepSearch().search_run("query", category="invalid"))
    with pytest.raises(ValueError, match="safe-search"):
        asyncio.run(DeepSearch().search_run("query", safesearch="invalid"))
    with pytest.raises(ValueError, match="time limit"):
        asyncio.run(DeepSearch().search_run("query", timelimit="invalid"))
    with pytest.raises(ValueError):
        DeepSearch(max_download_bytes=0)


def test_search_and_fetch_concurrency_have_independent_defaults():
    assert DeepSearch.DEFAULT_SEARCH_CONCURRENCY == 6
    assert DeepSearch.DEFAULT_FETCH_CONCURRENCY == 4
    engine = DeepSearch()
    assert engine.max_search_concurrency == 6
    assert engine.max_fetch_concurrency == 4
    search_sem = engine._search_semaphore()
    fetch_sem = engine._fetch_semaphore()
    assert search_sem is not fetch_sem


def test_concurrency_constructor_params_are_validated():
    with pytest.raises(ValueError):
        DeepSearch(max_search_concurrency=0)
    with pytest.raises(ValueError):
        DeepSearch(max_fetch_concurrency=0)


def test_max_concurrency_alias_targets_search_slot():
    engine = DeepSearch(max_concurrency=3)
    assert engine.max_search_concurrency == 3
    assert engine.max_fetch_concurrency == DeepSearch.DEFAULT_FETCH_CONCURRENCY


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
        engine = DeepSearch(max_search_concurrency=2, max_fetch_concurrency=3)
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
        asyncio.run(DeepSearch()._validate_public_url("http://example.test"))


def test_ssrf_guard_blocks_unsafe_schemes_and_credentials():
    with pytest.raises(ValueError, match="HTTP"):
        asyncio.run(DeepSearch()._validate_public_url("file:///etc/passwd"))
    with pytest.raises(ValueError, match="credentials"):
        asyncio.run(DeepSearch()._validate_public_url("https://user:secret@example.com"))


def test_public_network_is_allowed(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        ],
    )
    asyncio.run(DeepSearch()._validate_public_url("https://example.com"))


def test_download_limit_is_enforced(monkeypatch):
    async def allow(_self, _url):
        return None

    monkeypatch.setattr(DeepSearch, "_validate_public_url", allow)

    async def download():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"too large"
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await DeepSearch(max_download_bytes=3)._download(client, "https://example.com")

    with pytest.raises(ValueError, match="download limit"):
        asyncio.run(download())


def test_unsupported_content_type_is_blocked(monkeypatch):
    async def allow(_self, _url):
        return None

    monkeypatch.setattr(DeepSearch, "_validate_public_url", allow)

    async def download():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/octet-stream"}, content=b"data"
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            await DeepSearch()._download(client, "https://example.com")

    with pytest.raises(ValueError, match="unsupported content type"):
        asyncio.run(download())


def test_empty_trafilatura_result_uses_safe_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace(extract=lambda *a, **k: None))
    content, method = DeepSearch._extract(
        "<html><script>ignore()</script><main>Hello world</main></html>",
        "https://example.com",
    )
    assert content == "Hello world"
    assert method == "html-text-fallback"


def test_known_ad_redirects_are_filtered_without_substring_false_positive():
    assert DeepSearch._is_ad_redirect("https://www.google.com/aclick?id=1")
    assert DeepSearch._is_ad_redirect("https://ad.doubleclick.net/path")
    assert not DeepSearch._is_ad_redirect(
        "https://example.com/article?topic=doubleclick.net"
    )
    assert not DeepSearch._is_ad_redirect("https://notgoogle.com/aclick?id=1")


def test_provider_failures_are_reported_without_hidden_retry(monkeypatch):
    class BrokenDDGS:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            self.__class__.calls += 1
            raise RuntimeError("blocked")

    monkeypatch.setattr("deep_search.engine.DDGS", BrokenDDGS)
    run = asyncio.run(DeepSearch(backends=("broken",)).search_run("query", limit=3))
    assert run.results == []
    assert run.status == "failed"
    assert run.providers[0].provider == "broken"
    assert "RuntimeError: blocked" in run.providers[0].error
    assert BrokenDDGS.calls == 1


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

    monkeypatch.setattr("deep_search.engine.DDGS", CapturingDDGS)
    run = asyncio.run(
        DeepSearch(backends=("test",)).search_run(
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
        "backend": "test",
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
    results, status = asyncio.run(DeepSearch().github_run("agent search", 5))
    assert status.ok
    assert results[0].sources == ["github-api"]
    assert captured["params"] == {"q": "agent search", "per_page": 5}


def test_github_is_opt_in_and_fused_once(monkeypatch):
    engine = DeepSearch()
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
    assert combined.results[0].score == round(2 / 61, 6)
    assert combined.results[1].score == round(1 / 61, 6)


def test_diagnose_probes_each_backend_group_host_with_bare_get(monkeypatch):
    seen = []

    async def fake_get(self, url, **kwargs):
        seen.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(DeepSearch().diagnose_run()).dict()

    expected = {DeepSearch.BACKEND_HOSTS["github-api"]}
    for group in DeepSearch.DEFAULT_BACKENDS:
        expected |= {DeepSearch.BACKEND_HOSTS[name] for name in group.split(",")}
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
    payload = asyncio.run(DeepSearch().diagnose_run()).dict()
    entry = next(item for item in payload["providers"] if item["provider"] == "bing")
    assert entry["ok"] is True
    assert entry["http_status"] == 403


def test_diagnose_classifies_blocked_hosts(monkeypatch):
    hosts_to_exc = {
        "www.bing.com": httpx.ReadTimeout("timed out"),
        "www.google.com": httpx.ConnectError("refused", request=httpx.Request("GET", "u")),
        "yandex.com": _dns_connect_error(),
        "www.mojeek.com": RuntimeError("boom"),
        "www.startpage.com": _tls_connect_error(),
    }

    def exc_for(url):
        return hosts_to_exc.get(urlsplit(url).hostname)

    async def fake_get(self, url, **kwargs):
        exc = exc_for(url)
        if exc is not None:
            raise exc
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    payload = asyncio.run(DeepSearch().diagnose_run()).dict()

    by_provider = {entry["provider"]: entry for entry in payload["providers"]}
    assert by_provider["bing"]["error_kind"] == "timeout"
    assert by_provider["google"]["error_kind"] == "network"
    assert by_provider["yandex"]["error_kind"] == "dns"
    assert by_provider["startpage"]["error_kind"] == "tls"
    assert by_provider["mojeek"]["error_kind"] == "unknown"
    for provider in ("bing", "google", "yandex", "startpage", "mojeek"):
        entry = by_provider[provider]
        assert entry["ok"] is False
        assert entry["http_status"] is None
        assert entry["error"]
    assert payload["status"] == "degraded"


def test_diagnose_reports_unknown_backends_instead_of_skipping(monkeypatch):
    async def fail_get(self, url, **kwargs):
        raise AssertionError("no probe should run")

    monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)
    payload = asyncio.run(DeepSearch(backends=("mystery",)).diagnose_run()).dict()
    by_provider = {entry["provider"]: entry for entry in payload["providers"]}
    assert set(by_provider) == {"mystery", "github-api"}
    assert by_provider["mystery"]["ok"] is False
    assert by_provider["mystery"]["error_kind"] == "unknown"
    assert payload["status"] == "failed"
