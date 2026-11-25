"""Yahoo's two results markups (#100).

The fixtures under tests/fixtures/yahoo/ are verbatim result items cut from
captures taken on 2026-11-26 through ddgs's own Yahoo engine: one query flips
between two pages. "relsrch" is the markup ddgs 9.16.1 parses; "grp-talgo" is
the one it read as an empty answer. Hermetic: the engine's socket is stubbed at
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


def _wire(body: bytes):
    def factory(**kwargs):
        from ddgs import DDGS

        ddgs = DDGS(timeout=5)
        engine = ddgs._get_engines("text", "yahoo")[0]
        engine.http_client.client = SimpleNamespace(
            request=lambda *a, **k: SimpleNamespace(
                status_code=200, content=body, text=body.decode("utf-8")
            )
        )
        return ddgs

    return factory


def _run(monkeypatch, backend: str, body: bytes):
    monkeypatch.setattr("josty.engine.DDGS", _wire(body))
    return asyncio.run(
        Josty(backends=(backend,), enable_cache=False).search_run("q", limit=5)
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
    assert "Linux" in rows[0]["title"]
    assert rows[0]["body"]


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
