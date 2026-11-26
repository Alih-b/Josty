"""Yahoo's two results markups (#100).

The fixtures under tests/fixtures/yahoo/ are verbatim result items cut from
captures taken on 2026-11-26 through ddgs's own Yahoo engine: one query flips
between two pages. "relsrch" is the markup ddgs 9.16.1 parses; "grp-talgo" is
the one it read as an empty answer. "grp-talgo-nested" is the second markup as it
is served for long result lists (#126): three items cut from one capture, each
nested inside the previous one. Hermetic: the engine's socket is stubbed at
the primp client, under the tap (AGENTS.md invariant 2).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from josty.engine import Josty
from josty.providers import _yahoo_variant_rows

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
FIRST_MARKUP = (FIXTURES / "relsrch.html").read_bytes()
SECOND_MARKUP = (FIXTURES / "grp-talgo.html").read_bytes()
NESTED_MARKUP = (FIXTURES / "grp-talgo-nested.html").read_bytes()


def _wire(backend: str, body: bytes):
    def factory(**kwargs):
        from ddgs import DDGS

        ddgs = DDGS(timeout=5)
        for engine in ddgs._get_engines("text", backend):
            engine.http_client.client = SimpleNamespace(
                request=lambda *a, **k: SimpleNamespace(
                    status_code=200, content=body, text=body.decode("utf-8")
                ),
                set_cookies=lambda *a, **k: None,
            )
        return ddgs

    return factory


def _run(monkeypatch, backend: str, body: bytes, limit: int = 5):
    monkeypatch.setattr("josty.engine.DDGS", _wire(backend, body))
    return asyncio.run(
        Josty(backends=(backend,), enable_cache=False).search_run("q", limit=limit)
    )


@pytest.mark.parametrize("fixture", ["relsrch", "grp-talgo"], ids=["ddgs-markup", "second-markup"])
def test_both_markups_yield_results_through_the_real_engine(monkeypatch, fixture):
    body = FIRST_MARKUP if fixture == "relsrch" else SECOND_MARKUP

    run = _run(monkeypatch, "yahoo", body)

    assert len(run.results) >= 1
    provider = run.providers[0]
    assert provider.ok is True
    assert provider.error_kind is None
    assert provider.http_status == 200
    assert provider.body_bytes == len(body)
    assert run.status == "complete"
    # The second markup is read from bytes the tap already holds: no retry.
    assert run.request_count == 1
    assert all(result.sources == ["yahoo"] for result in run.results)
    assert all(result.url.startswith("https://") for result in run.results)


def test_the_second_markup_is_read_from_the_capture_not_invented():
    """The fixture is the kernel.org result of "Linux kernel first release year"."""
    rows = _yahoo_variant_rows(SECOND_MARKUP.decode("utf-8"))

    assert len(rows) == 1
    assert rows[0]["href"] == "https://www.kernel.org/category/releases.html"
    # The site name and display URL above the title are not part of it.
    assert rows[0]["title"] == "The Linux Kernel Archives - Releases"
    assert rows[0]["body"]


def test_the_second_markup_stops_at_the_call_limit(monkeypatch):
    """ddgs stops at max_results; rows past the limit must not reach fusion."""
    item = (
        '<section class="dd algo s-algo"><div class="compTitle"><h3>'
        '<a href="https://r.search.yahoo.com/_ylt=x/RU=https%3a%2f%2fexample.org%2f{n}'
        '/RK=2/RS=x">Result {n}</a></h3></div><div class="compText">s</div></section>'
    )
    page = "<html><body>" + "".join(item.format(n=n) for n in range(4)) + "</body></html>"

    run = _run(monkeypatch, "yahoo", page.encode(), limit=2)

    assert run.providers[0].result_count == 2
    assert [result.url for result in run.results] == [
        "https://example.org/0",
        "https://example.org/1",
    ]


def test_modules_without_a_result_link_are_dropped():
    """Ads and the AI and knowledge modules carry no /RU= redirect."""
    page = (
        '<html><body><section class="dd algo s-algo">'
        '<div class="compTitle"><h3>Ad</h3>'
        '<a href="https://www.bing.com/aclick?x=1">Ad</a></div>'
        '<div class="compText">buy</div></section></body></html>'
    )

    assert _yahoo_variant_rows(page) == []


def test_the_second_markup_is_yahoo_only(monkeypatch):
    """Another engine serving the same bytes is not re-parsed with Yahoo's selectors."""
    run = _run(monkeypatch, "brave", SECOND_MARKUP)

    # The stub answered, not the network.
    assert run.providers[0].http_status == 200
    assert run.providers[0].body_bytes == len(SECOND_MARKUP)
    assert run.results == []
    assert run.status != "complete"


def test_a_page_neither_markup_reads_is_parse_never_empty(monkeypatch):
    """A real title, no result markup of either kind, and large: a parser miss."""
    page = (
        b"<html><head><title>q - Yahoo Search Results</title></head><body>"
        + b"<p>lorem ipsum dolor</p>" * 9000
        + b"</body></html>"
    )

    run = _run(monkeypatch, "yahoo", page)

    assert run.providers[0].error_kind == "parse"
    assert run.status == "failed"


def test_nested_items_keep_only_their_own_title_and_text():
    """#126: each section.s-algo sits inside the previous one's grp-talgo-ext."""
    rows = _yahoo_variant_rows(NESTED_MARKUP.decode("utf-8"))

    assert [row["title"] for row in rows] == [
        "Coroutines and tasks — Python 3.15.2 documentation",
        "asyncio — Asynchronous I/O — Python 3.15.2 documentation",
        "How to use asyncio.TaskGroup – SuperFastPython",
    ]
    assert [row["href"] for row in rows] == [
        "https://docs.python.org/3/library/asyncio-task.html",
        "https://docs.python.org/3/library/asyncio.html",
        "https://superfastpython.com/asyncio-taskgroup/",
    ]
    assert all(len(row["body"]) < 400 for row in rows)
    assert not any(rows[0]["body"] == row["body"] for row in rows[1:])


def test_nested_page_yields_short_titles_through_the_real_engine(monkeypatch):
    run = _run(monkeypatch, "yahoo", NESTED_MARKUP)

    assert len(run.results) == 3
    assert max(len(item.title) for item in run.results) < 80
