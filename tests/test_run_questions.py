"""The questions a run answers (#106): why, more, only, agree, absent.

All but more() are views over what the run already holds. more() issues one
call to the first untried engine. Hermetic: ddgs is replaced per test, and
Mwmbl's one wire call is patched (AGENTS.md invariant 2).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from josty.engine import Josty
from josty.models import ProviderStatus, SearchResult, SearchRun
from josty.providers import MwmblSearchAdapter

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


class _Empty:
    def __init__(self, **kwargs):
        pass

    def text(self, query, **kwargs):
        return []


def _wire(status: int):
    """A real ddgs client whose duckduckgo socket answers with one status.

    duckduckgo, not brave: brave's engine also asks its client for cookies.
    """

    def factory(**kwargs):
        from ddgs import DDGS

        ddgs = DDGS(timeout=5)
        engine = ddgs._get_engines("text", "duckduckgo")[0]
        answer = SimpleNamespace(status_code=status, content=b"", text="")
        engine.http_client.client = SimpleNamespace(request=lambda *a, **k: answer)
        return ddgs

    return factory


def _run(monkeypatch, backends, **kwargs):
    engine = Josty(backends=backends, enable_cache=False)
    return asyncio.run(engine.search_run("q", limit=5, **kwargs))


def test_why_is_a_problem_with_a_stable_fragment_and_the_wire_status(monkeypatch):
    monkeypatch.setattr("josty.engine.DDGS", _wire(429))

    run = _run(monkeypatch, ("duckduckgo",))

    problem = run.why("duckduckgo")
    assert problem["status"] == 429
    assert problem["type"].endswith("#rate-limited")
    assert problem["engine"] == "duckduckgo"
    assert "HTTP 429" in problem["detail"]


def test_why_separates_answered_untried_and_blocked():
    answered = ProviderStatus("yandex", "q", True, 3)
    blocked = ProviderStatus(
        "brave", "q", False, 0, error_kind="blocked", http_status=403, error="HTTP 403"
    )
    run = SearchRun("q", providers=[answered, blocked], untried=["mojeek"])

    assert run.why("yandex") is None
    assert run.why("nosuch") is None
    assert run.why("mojeek")["type"].endswith("#untried")
    assert run.why("brave")["type"].endswith("#blocked")
    assert run.why("brave")["status"] == 403


def test_absent_separates_an_index_with_nothing_from_no_answer(monkeypatch):
    monkeypatch.setattr("josty.engine.DDGS", _Empty)
    asked_all = _run(monkeypatch, ("duckduckgo",))
    assert asked_all.absent.kind == "novalue"
    assert asked_all.absent.answered == ["duckduckgo"]

    monkeypatch.setattr("josty.engine.DDGS", _wire(429))
    none_answered = _run(monkeypatch, ("duckduckgo",))
    assert none_answered.absent.kind == "somevalue"
    assert none_answered.absent.answered == []
    assert none_answered.absent.asked == ["duckduckgo"]

    monkeypatch.setattr("josty.engine.DDGS", _Rows())
    assert _run(monkeypatch, ("duckduckgo",)).absent is None


def test_more_asks_exactly_one_untried_engine_and_leaves_the_run_alone(monkeypatch):
    ddgs = _Rows()
    monkeypatch.setattr("josty.engine.DDGS", ddgs)

    async def mwmbl_rows(self, client, query):
        return MWMBL_ROWS

    monkeypatch.setattr(MwmblSearchAdapter, "_request", mwmbl_rows)
    run = _run(monkeypatch, ("yandex", "mwmbl", "bing", "yahoo"))
    # yandex and bing are the two indexes; mwmbl's rows do not count.
    assert run.untried == ["yahoo"]
    before = (run.request_count, list(run.untried), len(run.results), run.dict())

    more = asyncio.run(run.more())

    assert ddgs.calls == ["yandex", "bing", "yahoo"]
    assert more.request_count == 1
    assert [source for r in more.results for source in r.sources] == ["yahoo"]
    assert [p.provider for p in more.providers] == ["yahoo"]
    assert more.untried == []
    assert (run.request_count, list(run.untried), len(run.results), run.dict()) == before
    assert asyncio.run(more.more()) is None


def test_only_slices_by_engine_or_index_and_agree_counts_distinct_indexes():
    results = [
        SearchResult("a", "https://example.org/a", "", sources=["yandex"]),
        SearchResult("b", "https://example.org/b", "", sources=["yandex", "yahoo"]),
        SearchResult("c", "https://example.org/c", "", sources=["duckduckgo", "yahoo"]),
    ]
    run = SearchRun("q", results=results)

    yandex = ["https://example.org/a", "https://example.org/b"]
    bing = ["https://example.org/b", "https://example.org/c"]
    assert [r.url for r in run.only("yandex")] == yandex
    # yahoo and duckduckgo are both Bing's index.
    assert [r.url for r in run.only("bing")] == bing
    assert [r.url for r in run.agree(2)] == ["https://example.org/b"]
    assert run.agree(3) == []
