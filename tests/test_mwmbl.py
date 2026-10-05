"""The keyless Mwmbl source: filter honesty, one call, bounded rows.

Hermetic: the wire call is patched, so nothing here touches the network
(AGENTS.md invariant 2). The autouse stub in ``tests/conftest.py`` already
answers Mwmbl locally; these tests replace it with a canned payload.
"""

from __future__ import annotations

import asyncio

import pytest

from josty.branch import IssueContext, SearchCall
from josty.fanout import _FanoutLedger
from josty.models import SearchResult
from josty.providers import MwmblSearchAdapter

BODY = {
    "query": "q",
    "number_of_results": 3,
    "results": [
        {"url": "https://sqlite.org/forum/x", "title": "A", "content": "one"},
        {"url": "https://example.org/b", "title": "B", "extract": "two"},
        {"url": "", "title": "no url", "content": "dropped"},
    ],
}


def _call(**overrides) -> SearchCall:
    fields = {
        "query": "q",
        "backend": "mwmbl",
        "limit": 20,
        "category": "text",
        "region": None,
        "safesearch": "moderate",
        "timelimit": None,
    }
    fields.update(overrides)
    return SearchCall(**fields)


def _patch_body(monkeypatch, body: object) -> None:
    async def canned(self, client, query):
        assert query == "q"
        return body

    monkeypatch.setattr(MwmblSearchAdapter, "_request", canned)


def test_precheck_allows_only_a_plain_text_query():
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
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
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    reason = adapter.precheck(_call(**overrides))
    assert reason is not None and reason.startswith("skipped:")


def test_issue_parses_rows_and_counts_exactly_one_call(monkeypatch):
    _patch_body(monkeypatch, BODY)
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    ledger = _FanoutLedger()
    ctx = IssueContext(
        ledger=ledger, pool=None, lease=None, budget=5.0, executor=None
    )

    outcome = asyncio.run(adapter.issue(_call(), ctx))

    assert outcome.error is None
    assert ledger.issued == 1
    # The row without a URL is dropped, not rendered as an empty result.
    assert [r.url for r in outcome.results] == [
        "https://sqlite.org/forum/x",
        "https://example.org/b",
    ]
    assert outcome.results[0].snippet == "one"
    assert outcome.results[1].snippet == "two"
    assert [r.engine_ranks for r in outcome.results] == [
        {"mwmbl": 1},
        {"mwmbl": 2},
    ]
    assert all(r.sources == ["mwmbl"] for r in outcome.results)


def test_issue_truncates_to_the_call_limit(monkeypatch):
    body = {
        "results": [
            {"url": f"https://example.org/{i}", "title": str(i), "content": "x"}
            for i in range(10)
        ]
    }
    _patch_body(monkeypatch, body)
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    ctx = IssueContext(
        ledger=_FanoutLedger(), pool=None, lease=None, budget=5.0, executor=None
    )

    outcome = asyncio.run(adapter.issue(_call(limit=3), ctx))

    assert len(outcome.results) == 3


def test_issue_returns_a_usable_empty_response_for_a_non_dict_body(monkeypatch):
    _patch_body(monkeypatch, [{"unexpected": "shape"}])
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    ctx = IssueContext(
        ledger=_FanoutLedger(), pool=None, lease=None, budget=5.0, executor=None
    )

    outcome = asyncio.run(adapter.issue(_call(), ctx))

    assert outcome.error is None
    assert outcome.results == []


@pytest.mark.parametrize("body", [{}, {"results": None}, {"results": 42}, {"results": "abc"}])
def test_issue_treats_an_unusable_results_field_as_empty(monkeypatch, body):
    _patch_body(monkeypatch, body)
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    ctx = IssueContext(
        ledger=_FanoutLedger(), pool=None, lease=None, budget=5.0, executor=None
    )

    outcome = asyncio.run(adapter.issue(_call(), ctx))

    assert outcome.error is None
    assert outcome.results == []


@pytest.mark.parametrize("url", [123, {"a": 1}, ["x"], True, None, ""])
def test_issue_drops_a_row_whose_url_is_not_a_usable_string(monkeypatch, url):
    """A shape variant upstream must not raise on the path to stdout.

    A non-string URL used to reach ``ranking.canonical`` and raise
    ``AttributeError``, which escaped the CLI as exit 1 with no JSON.
    """
    _patch_body(monkeypatch, {"results": [{"url": url, "title": "t", "content": "c"}]})
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    ctx = IssueContext(
        ledger=_FanoutLedger(), pool=None, lease=None, budget=5.0, executor=None
    )

    outcome = asyncio.run(adapter.issue(_call(), ctx))

    assert outcome.error is None
    assert outcome.results == []


def test_the_default_roster_queries_mwmbl_but_keeps_primary_results(monkeypatch):
    """Pins the integration the adapter alone cannot: roster, dispatch, fallback."""
    from josty import Josty

    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            return [
                {"title": "Hit", "href": "https://example.com/doc", "body": "Snippet"}
            ]

        news = text

    monkeypatch.setattr("josty.engine.DDGS", FakeDDGS)
    monkeypatch.setattr(
        "josty.engine._engine_available", lambda category, backend: (True, None)
    )
    _patch_body(monkeypatch, BODY)

    run = asyncio.run(Josty(enable_cache=False).search_run("q", limit=5))

    assert "mwmbl" in {p.provider for p in run.providers}
    # Six ddgs engines plus the native source: one call each, no amplification.
    assert run.request_count == 7
    assert run.scheduled_count == 7
    assert run.shed_count == 0
    # A primary engine answered, so the fallback source's rows are not fused:
    # the order above it is exactly what it would be without the source.
    assert "mwmbl" not in {source for r in run.results for source in r.sources}


def test_mwmbl_rows_answer_a_run_the_primary_engines_left_empty(monkeypatch):
    """The case the source exists for: every ddgs engine returns nothing."""
    from josty import Josty

    class EmptyDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            return []

        news = text

    monkeypatch.setattr("josty.engine.DDGS", EmptyDDGS)
    monkeypatch.setattr(
        "josty.engine._engine_available", lambda category, backend: (True, None)
    )
    _patch_body(monkeypatch, BODY)

    run = asyncio.run(Josty(enable_cache=False).search_run("q", limit=5))

    assert run.status != "empty"
    assert [r.url for r in run.results] == [
        "https://sqlite.org/forum/x",
        "https://example.org/b",
    ]
    assert all(r.sources == ["mwmbl"] for r in run.results)
    assert run.request_count == 7


def test_prefer_primary_drops_a_fallback_list_only_when_a_primary_has_rows():
    from josty.ranking import prefer_primary

    def item(url: str, source: str) -> SearchResult:
        return SearchResult(title="t", url=url, sources=[source])

    primary = [item("https://example.com/a", "yahoo")]
    fallback = [item("https://example.org/b", "mwmbl")]

    assert prefer_primary([primary, fallback], frozenset({"mwmbl"})) == [primary]
    assert prefer_primary([fallback], frozenset({"mwmbl"})) == [fallback]
    assert prefer_primary([primary], frozenset({"mwmbl"})) == [primary]
    assert prefer_primary([], frozenset({"mwmbl"})) == []
    # No fallback configured: nothing is ever dropped.
    assert prefer_primary([primary, fallback], frozenset()) == [primary, fallback]


@pytest.mark.parametrize("field", ["title", "content", "extract"])
def test_issue_coerces_a_non_string_text_field(monkeypatch, field):
    """A scalar title or snippet must not reach `len()` in ranking._merge_result."""
    _patch_body(monkeypatch, {"results": [{"url": "https://example.org/a", field: 42}]})
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    ctx = IssueContext(
        ledger=_FanoutLedger(), pool=None, lease=None, budget=5.0, executor=None
    )

    outcome = asyncio.run(adapter.issue(_call(), ctx))

    assert outcome.error is None
    assert len(outcome.results) == 1
    assert isinstance(outcome.results[0].title, str)
    assert isinstance(outcome.results[0].snippet, str)


def test_issue_bounds_the_whole_call_by_the_run_budget(monkeypatch):
    """A trickling upstream must not hold the branch past its budget.

    Per-operation HTTPX timeouts alone do not bound total wall time, so the
    adapter awaits the whole fetch under ``asyncio.wait_for``.
    """
    import time as _time

    async def slow_fetch(self, query, budget, headers):
        await asyncio.sleep(5)
        return {"results": []}

    monkeypatch.setattr(MwmblSearchAdapter, "_fetch", slow_fetch)
    adapter = MwmblSearchAdapter(timeout=8, user_agent="test-agent")
    ctx = IssueContext(
        ledger=_FanoutLedger(), pool=None, lease=None, budget=0.05, executor=None
    )

    t0 = _time.perf_counter()
    outcome = asyncio.run(adapter.issue(_call(), ctx))
    elapsed = _time.perf_counter() - t0

    assert outcome.error is not None
    assert outcome.results == []
    assert elapsed < 2, f"budget was not enforced: {elapsed:.2f}s"
