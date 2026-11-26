"""The keyless Wikipedia ``list=search`` source (#102).

Hermetic: the wire call is patched, so nothing here touches the network
(AGENTS.md invariant 2). The autouse stub in ``tests/conftest.py`` already
answers Wikipedia locally; these tests replace it with a canned payload.
"""

from __future__ import annotations

import asyncio

import pytest

from josty.branch import IssueContext, SearchCall
from josty.engine import Josty
from josty.errors import ProviderError
from josty.fanout import _FanoutLedger
from josty.providers import WIKIPEDIA_SEARCH_URL, WikipediaSearchAdapter

BODY = {
    "batchcomplete": "",
    "query": {
        "searchinfo": {"totalhits": 2},
        "search": [
            {
                "ns": 0,
                "title": "Asyncio",
                "snippet": (
                    '<span class="searchmatch">Asynchronous</span> I/O &amp; events'
                ),
            },
            {"ns": 0, "title": "C Sharp (programming language)", "snippet": "plain"},
            {"ns": 0, "title": "", "snippet": "dropped: no title"},
            {"ns": 0, "snippet": "dropped: title is not a string"},
        ],
    },
}


def _page(hits: list[dict], total: int | None = None) -> dict:
    total = len(hits) if total is None else total
    return {"query": {"searchinfo": {"totalhits": total}, "search": hits}}


def _call(**overrides) -> SearchCall:
    fields = {
        "query": "q",
        "backend": "wikipedia",
        "limit": 20,
        "category": "text",
        "region": None,
        "safesearch": "moderate",
        "timelimit": None,
    }
    fields.update(overrides)
    return SearchCall(**fields)


def _ctx() -> IssueContext:
    return IssueContext(
        ledger=_FanoutLedger(), pool=None, lease=None, budget=5.0, executor=None
    )


def _patch_body(monkeypatch, body: object) -> None:
    async def canned(self, client, query, limit):
        assert query == "q"
        return body

    monkeypatch.setattr(WikipediaSearchAdapter, "_request", canned)


def _issue(monkeypatch, body: object, call: SearchCall | None = None):
    _patch_body(monkeypatch, body)
    adapter = WikipediaSearchAdapter(timeout=8, user_agent="test-agent")
    return asyncio.run(adapter.issue(call or _call(), _ctx()))


def test_precheck_allows_only_a_plain_text_query():
    adapter = WikipediaSearchAdapter(timeout=8, user_agent="test-agent")
    assert adapter.precheck(_call()) is None
    assert adapter.precheck(_call(safesearch="off")) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "news"},
        {"timelimit": "w"},
        {"region": "de-de"},
        {"safesearch": "on"},
    ],
)
def test_precheck_skips_a_filter_the_source_cannot_honour(overrides):
    adapter = WikipediaSearchAdapter(timeout=8, user_agent="test-agent")
    reason = adapter.precheck(_call(**overrides))
    assert reason is not None and reason.startswith("not applicable:")


# Captured at import, before the autouse stub in conftest replaces the method.
_REAL_REQUEST = WikipediaSearchAdapter.__dict__["_request"]


def test_request_asks_list_search_and_never_opensearch():
    asked: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return _page([])

    class FakeClient:
        async def get(self, url, params=None):
            asked.update(url=url, params=params)
            return FakeResponse()

    adapter = WikipediaSearchAdapter(timeout=8, user_agent="test-agent")

    asyncio.run(_REAL_REQUEST(adapter, FakeClient(), "python asyncio timeout", 10))

    assert asked["url"] == WIKIPEDIA_SEARCH_URL
    params = asked["params"]
    assert params["action"] == "query"
    assert params["list"] == "search"
    assert params["srsearch"] == "python asyncio timeout"
    assert params["srlimit"] == 10
    # The prefix-only endpoint ddgs uses is the bug this source exists for.
    assert "opensearch" not in str(asked)


@pytest.mark.parametrize(
    ("page", "kind"),
    [
        ("<html><head><title>Wikimedia Error</title></head></html>", "parse"),
        ("<html><head><title>Just a moment...</title></head></html>", "blocked"),
    ],
)
def test_a_200_that_is_not_json_is_parse_or_blocked_never_unknown(page, kind):
    class HtmlResponse:
        status_code = 200
        text = page

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class FakeClient:
        async def get(self, url, params=None):
            return HtmlResponse()

    adapter = WikipediaSearchAdapter(timeout=8, user_agent="test-agent")

    with pytest.raises(ProviderError) as raised:
        asyncio.run(_REAL_REQUEST(adapter, FakeClient(), "q", 10))

    assert raised.value.error_kind == kind


def test_issue_maps_rows_to_canonical_wiki_urls_and_counts_one_call(monkeypatch):
    _patch_body(monkeypatch, BODY)
    ledger = _FanoutLedger()
    ctx = IssueContext(ledger=ledger, pool=None, lease=None, budget=5.0, executor=None)

    outcome = asyncio.run(WikipediaSearchAdapter(timeout=8, user_agent="a").issue(_call(), ctx))

    assert outcome.error is None
    assert ledger.issued == 1
    # Rows without a usable title are dropped, not rendered as empty results.
    assert [r.url for r in outcome.results] == [
        "https://en.wikipedia.org/wiki/Asyncio",
        "https://en.wikipedia.org/wiki/C_Sharp_(programming_language)",
    ]
    # The API's highlight tags and entities do not reach the snippet.
    assert outcome.results[0].snippet == "Asynchronous I/O & events"
    assert [r.engine_ranks for r in outcome.results] == [{"wikipedia": 1}, {"wikipedia": 2}]
    assert all(r.sources == ["wikipedia"] for r in outcome.results)


def test_the_call_limit_reaches_the_api(monkeypatch):
    """A run asking for 25 rows asks Wikipedia for 25, not a fixed 10."""
    asked: list[int] = []

    async def counted(self, client, query, limit):
        asked.append(limit)
        return _page([])

    monkeypatch.setattr(WikipediaSearchAdapter, "_request", counted)
    adapter = WikipediaSearchAdapter(timeout=8, user_agent="test-agent")

    asyncio.run(adapter.issue(_call(limit=25), _ctx()))

    assert asked == [25]


def test_issue_respects_a_small_limit(monkeypatch):
    outcome = _issue(monkeypatch, BODY, _call(limit=1))

    assert outcome.error is None
    assert len(outcome.results) == 1


def test_zero_totalhits_is_a_certified_empty_not_a_block(monkeypatch):
    outcome = _issue(monkeypatch, _page([], total=0))

    assert outcome.error is None
    assert outcome.results == []

    run = asyncio.run(Josty(backends=("wikipedia",), enable_cache=False).search_run("q"))

    assert run.status == "empty"
    assert (run.providers[0].ok, run.providers[0].error_kind) == (True, "empty")


@pytest.mark.parametrize(
    ("body", "total"),
    [
        (_page([], total=3), "3"),
        ({"query": {"search": []}}, "None"),
        ([{"unexpected": "shape"}], "None"),
        ({}, "None"),
    ],
)
def test_no_rows_is_nothing_found_only_when_totalhits_says_so(monkeypatch, body, total):
    """Hits reported but none returned is a parse problem; absent is not a certificate."""
    outcome = _issue(monkeypatch, body)

    assert outcome.results == []
    assert isinstance(outcome.error, ProviderError)
    assert outcome.error.error_kind == "parse"
    assert f"totalhits={total}" in str(outcome.error)


def test_positive_totalhits_without_rows_fails_as_parse(monkeypatch):
    _patch_body(monkeypatch, _page([], total=3))

    run = asyncio.run(Josty(backends=("wikipedia",), enable_cache=False).search_run("q"))

    provider = run.providers[0]
    assert run.status == "failed"
    assert provider.ok is False
    assert provider.error_kind == "parse"
    assert "totalhits=3" in provider.error


@pytest.mark.parametrize("overrides", [{"timelimit": "w"}, {"region": "de-de"}])
def test_a_filter_it_cannot_honour_is_not_applicable_without_a_call(overrides):
    run = asyncio.run(
        Josty(backends=("wikipedia",), enable_cache=False).search_run("q", **overrides)
    )

    provider = run.providers[0]
    assert provider.error_kind == "not_applicable"
    assert provider.error.startswith("not applicable:")
    assert run.request_count == 0
    assert run.partial is False


def test_wikipedia_is_a_native_source_and_never_goes_through_ddgs(monkeypatch):
    class ExplodingDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise AssertionError("wikipedia must not be asked through ddgs")

    monkeypatch.setattr("josty.engine.DDGS", ExplodingDDGS)
    assert "wikipedia" in Josty.NATIVE_BACKENDS

    run = asyncio.run(Josty(backends=("wikipedia",), enable_cache=False).search_run("q"))

    assert run.providers[0].provider == "wikipedia"
    assert run.providers[0].ok is True
    assert run.providers[0].error_kind == "empty"
    assert run.request_count == 1
