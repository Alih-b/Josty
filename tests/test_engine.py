import asyncio
import socket

import httpx
import pytest
from deep_search.engine import DeepSearch, SearchResult, canonical, rrf


def test_canonical_removes_tracking_query_fragment_and_default_port():
    assert canonical("HTTPS://Example.COM:443/path/?utm_source=x#part") == "https://example.com/path"


def test_canonical_preserves_resource_query_and_removes_only_tracking_keys():
    assert canonical(
        "https://example.com/search?q=python&utm_campaign=test&page=2&fbclid=abc"
    ) == "https://example.com/search?q=python&page=2"
    assert canonical("https://example.com/search?q=rust") != canonical(
        "https://example.com/search?q=python"
    )


def test_canonical_preserves_non_default_port():
    assert canonical("http://Example.com:8080/") == "http://example.com:8080/"


def test_rrf_deduplicates_within_and_across_lists():
    first = [
        SearchResult("A", "https://example.com/a?utm_source=test", "short"),
        SearchResult("A duplicate", "https://example.com/a", "longer snippet"),
    ]
    second = [SearchResult("A again", "https://EXAMPLE.com/a/", "best and longest snippet")]
    results = rrf([first, second])
    assert len(results) == 1
    assert results[0].snippet == "best and longest snippet"
    assert results[0].score == round(2 / 61, 6)


def test_rrf_rejects_invalid_k():
    with pytest.raises(ValueError):
        rrf([], k=0)


def test_rrf_skips_malformed_urls():
    malformed = SearchResult("bad", "https://example.com:invalid/path")
    assert rrf([[malformed]]) == []


def test_plain_expansion_does_not_contaminate_general_queries():
    assert DeepSearch.expand("company revenue history") == ["company revenue history"]


def test_oss_expansion_is_explicit():
    expanded = DeepSearch.expand("agent search", mode="oss")
    assert expanded == [
        "agent search",
        '"agent search"',
        "agent search open source",
        "agent search self-hosted",
    ]


def test_site_expansion_avoids_duplicate_operator():
    assert DeepSearch.expand("site:github.com agent search", ["github.com"]) == [
        "site:github.com agent search"
    ]


def test_invalid_limit_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(DeepSearch().search_run("query", limit=101))


def test_private_network_is_blocked(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
    )
    with pytest.raises(ValueError, match="private or reserved"):
        asyncio.run(DeepSearch._validate_public_url("http://example.test"))


def test_public_network_is_allowed(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        ],
    )
    asyncio.run(DeepSearch._validate_public_url("https://example.com"))


def test_non_http_url_is_blocked():
    with pytest.raises(ValueError, match="HTTP"):
        asyncio.run(DeepSearch._validate_public_url("file:///etc/passwd"))


def test_url_credentials_are_blocked():
    with pytest.raises(ValueError, match="credentials"):
        asyncio.run(DeepSearch._validate_public_url("https://user:secret@example.com"))


def test_download_limit_is_enforced(monkeypatch):
    async def allow(_url):
        return None

    monkeypatch.setattr(DeepSearch, "_validate_public_url", staticmethod(allow))

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
    async def allow(_url):
        return None

    monkeypatch.setattr(DeepSearch, "_validate_public_url", staticmethod(allow))

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


def test_provider_failures_are_reported(monkeypatch):
    class BrokenDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise RuntimeError("blocked")

    monkeypatch.setattr("deep_search.engine.DDGS", BrokenDDGS)
    run = asyncio.run(
        DeepSearch(backends=("broken",), max_retries=0).search_run("query", limit=3)
    )
    assert run.results == []
    assert run.partial is True
    assert run.providers[0].provider == "broken"
    assert run.providers[0].attempts == 1
    assert "RuntimeError: blocked" in run.providers[0].error


def test_provider_retries_are_reported(monkeypatch):
    class FlakyDDGS:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            self.__class__.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return [{"title": "ok", "href": "https://example.com", "body": "found"}]

    monkeypatch.setattr("deep_search.engine.DDGS", FlakyDDGS)
    run = asyncio.run(
        DeepSearch(backends=("test",), max_retries=1, retry_base_delay=0).search_run(
            "query", limit=3
        )
    )
    assert run.partial is False
    assert run.providers[0].attempts == 2
    assert run.results[0].url == "https://example.com"


def test_news_filters_are_forwarded(monkeypatch):
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
                }
            ]

    monkeypatch.setattr("deep_search.engine.DDGS", CapturingDDGS)
    run = asyncio.run(
        DeepSearch(backends=("test",), max_retries=0).search_run(
            "query",
            limit=3,
            category="news",
            region="de-de",
            safesearch="on",
            timelimit="w",
        )
    )
    assert run.results[0].url == "https://example.com/news"
    assert captured == {
        "query": "query",
        "backend": "test",
        "max_results": 3,
        "safesearch": "on",
        "region": "de-de",
        "timelimit": "w",
    }
