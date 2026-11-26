"""The engine walk (#105): ask in a measured order, stop at two indexes.

Hermetic: ddgs is replaced per test, and Mwmbl's one wire call is patched
(AGENTS.md invariant 2).
"""

from __future__ import annotations

import asyncio

import pytest

from josty.engine import Josty
from josty.providers import MwmblSearchAdapter
from josty.status import WANT_INDEXES, engine_walk_order, index_of

MWMBL_ROWS = {"results": [{"url": "https://sqlite.org/forum/x", "title": "A", "content": "one"}]}


class _Rows:
    """Answers every engine with one row that carries the engine's name."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, **kwargs):
        return self

    def text(self, query, **kwargs):
        backend = kwargs["backend"]
        self.calls.append(backend)
        return [{"title": backend, "href": f"https://example.org/{backend}"}]


def _use_ddgs(monkeypatch, ddgs) -> None:
    monkeypatch.setattr("josty.engine.DDGS", ddgs)


def _run(backends: tuple[str, ...], **kwargs):
    return asyncio.run(
        Josty(backends=backends, enable_cache=False).search_run("q", limit=5, **kwargs)
    )


def test_the_walk_stops_at_two_indexes_and_fuses_every_answer(monkeypatch):
    ddgs = _Rows()
    _use_ddgs(monkeypatch, ddgs)

    async def mwmbl_rows(self, client, query):
        return MWMBL_ROWS

    monkeypatch.setattr(MwmblSearchAdapter, "_request", mwmbl_rows)

    run = _run(("brave", "yahoo", "mwmbl", "yandex"))

    # yandex first, then mwmbl (native: its rows fuse, but it is no index that
    # ends the walk), then yahoo. brave is never asked.
    order = engine_walk_order(["brave", "yahoo", "mwmbl", "yandex"])
    assert order == ["yandex", "mwmbl", "yahoo", "brave"]
    assert ddgs.calls == ["yandex", "yahoo"]
    assert run.request_count == 3
    assert run.untried == ["brave"]
    assert run.status == "complete"
    sources = sorted({source for r in run.results for source in r.sources})
    assert sources == ["mwmbl", "yahoo", "yandex"]
    assert run.dict()["untried"] == ["brave"]

    from josty.cache import _search_run_from_dict

    assert _search_run_from_dict(run.dict()).untried == ["brave"]


def test_the_default_walk_reaches_a_second_real_index(monkeypatch):
    """mwmbl is asked and fused, and the walk still goes on to a real second index."""
    ddgs = _Rows()
    _use_ddgs(monkeypatch, ddgs)

    async def mwmbl_rows(self, client, query):
        return MWMBL_ROWS

    monkeypatch.setattr(MwmblSearchAdapter, "_request", mwmbl_rows)

    run = asyncio.run(Josty(enable_cache=False).search_run("q", limit=5))

    assert ddgs.calls == ["yandex", "yahoo"]
    assert run.request_count == 3
    assert {source for r in run.results for source in r.sources} == {"yandex", "mwmbl", "yahoo"}
    assert run.status == "complete"


def test_two_engines_on_one_index_do_not_satisfy_the_walk(monkeypatch):
    ddgs = _Rows()
    _use_ddgs(monkeypatch, ddgs)

    run = _run(("duckduckgo", "yahoo", "mojeek"))

    # yahoo and duckduckgo are both Bing's index, so mojeek is asked as well.
    assert index_of("duckduckgo") == index_of("yahoo") == "bing"
    assert WANT_INDEXES == 2
    assert ddgs.calls == ["yahoo", "duckduckgo", "mojeek"]
    assert run.untried == []


@pytest.mark.parametrize(
    "filters",
    [
        {"sites": ["github.com"]},
        {"region": "us-en"},
        {"timelimit": "w"},
        {"safesearch": "on"},
    ],
)
def test_a_source_that_cannot_honour_the_filter_is_not_a_loss(monkeypatch, filters):
    """Not asked is not failed: the run stays complete."""

    class OnSite:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            backend = kwargs["backend"]
            return [{"title": backend, "href": f"https://github.com/{backend}"}]

    _use_ddgs(monkeypatch, OnSite)

    async def never(self, client, query):
        raise AssertionError("a source that cannot honour the filter must not be asked")

    monkeypatch.setattr(MwmblSearchAdapter, "_request", never)

    run = _run(("mwmbl", "yandex", "yahoo"), **filters)

    refused = [p for p in run.providers if p.error_kind == "not_applicable"]
    assert [p.provider for p in refused] == ["mwmbl"]
    assert run.partial is False
    assert run.status == "complete"


def test_a_site_filter_is_not_sent_to_a_source_that_cannot_honour_it(monkeypatch):
    ddgs = _Rows()
    _use_ddgs(monkeypatch, ddgs)

    async def never(self, client, query):
        raise AssertionError("a source without a site: operator must not be asked")

    monkeypatch.setattr(MwmblSearchAdapter, "_request", never)

    run = _run(("mwmbl", "yandex"), sites=["github.com"])

    assert ddgs.calls == ["yandex"]
    refused = [p for p in run.providers if p.error_kind == "not_applicable"]
    assert [p.provider for p in refused] == ["mwmbl"]
    assert refused[0].error.startswith("not applicable:")
    assert run.partial is False
    assert run.request_count == 1
