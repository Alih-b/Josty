"""The engine walk (#105): ask in a measured order, stop at two indexes.

Hermetic: ddgs is replaced per test, and Mwmbl's one wire call is patched
(AGENTS.md invariant 2).
"""

from __future__ import annotations

import asyncio

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


def test_the_walk_stops_at_two_indexes_and_fuses_both(monkeypatch):
    ddgs = _Rows()
    _use_ddgs(monkeypatch, ddgs)

    async def mwmbl_rows(self, client, query):
        return MWMBL_ROWS

    monkeypatch.setattr(MwmblSearchAdapter, "_request", mwmbl_rows)

    run = _run(("brave", "yahoo", "mwmbl", "yandex"))

    # yandex first, then mwmbl (native, so not through ddgs). The rest is never asked.
    order = engine_walk_order(["brave", "yahoo", "mwmbl", "yandex"])
    assert order == ["yandex", "mwmbl", "yahoo", "brave"]
    assert ddgs.calls == ["yandex"]
    assert run.request_count == 2
    assert run.untried == ["yahoo", "brave"]
    assert run.status == "complete"
    assert sorted({source for r in run.results for source in r.sources}) == ["mwmbl", "yandex"]
    assert run.dict()["untried"] == ["yahoo", "brave"]

    from josty.cache import _search_run_from_dict

    assert _search_run_from_dict(run.dict()).untried == ["yahoo", "brave"]


def test_two_engines_on_one_index_do_not_satisfy_the_walk(monkeypatch):
    ddgs = _Rows()
    _use_ddgs(monkeypatch, ddgs)

    run = _run(("duckduckgo", "yahoo", "mojeek"))

    # yahoo and duckduckgo are both Bing's index, so mojeek is asked as well.
    assert index_of("duckduckgo") == index_of("yahoo") == "bing"
    assert WANT_INDEXES == 2
    assert ddgs.calls == ["yahoo", "duckduckgo", "mojeek"]
    assert run.untried == []


def test_a_site_filter_is_not_sent_to_a_source_that_cannot_honour_it(monkeypatch):
    ddgs = _Rows()
    _use_ddgs(monkeypatch, ddgs)

    async def never(self, client, query):
        raise AssertionError("a source without a site: operator must not be asked")

    monkeypatch.setattr(MwmblSearchAdapter, "_request", never)

    run = _run(("mwmbl", "yandex"), sites=["github.com"])

    assert ddgs.calls == ["yandex"]
    skipped = {p.provider: p.error for p in run.providers if p.error_kind == "skipped"}
    assert list(skipped) == ["mwmbl"]
    assert skipped["mwmbl"].startswith("skipped:")
    assert run.request_count == 1
