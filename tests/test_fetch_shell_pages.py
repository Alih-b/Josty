"""A page that needs JavaScript is a failed fetch, not a 33-character success (#130)."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from josty.cache import _stamp_fetch_stats
from josty.engine import Josty
from josty.fetch import MIN_FALLBACK_CHARS, extract
from josty.models import SearchResult, SearchRun

SHELL = (
    "<html><head><title>SAP Help Portal | SAP Online Help</title>"
    "<script>window.__APP__ = {}</script></head>"
    '<body><div id="app"></div><noscript>Please enable JavaScript</noscript></body></html>'
)


@pytest.fixture(autouse=True)
def _trafilatura_finds_nothing(monkeypatch):
    """What it returned for the live page: no article, so the tag-stripped fallback runs."""
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace(extract=lambda *a, **k: None))


def _fetch(monkeypatch, html: str) -> SearchResult:
    async def allow(_self, _url):
        return None

    async def fake_download(_self, client, url):
        return html, url

    monkeypatch.setattr(Josty, "_validate_public_url", allow)
    monkeypatch.setattr(Josty, "_download", fake_download)
    item = SearchResult("title", "https://help.example.org/page", "", sources=["test"])
    asyncio.run(Josty(enable_cache=False).fetch_content([item]))
    return item


def test_the_shell_page_really_falls_back_to_a_few_words():
    text, method = extract(SHELL, "https://help.example.org/page")

    assert method == "html-text-fallback"
    assert 0 < len(text) < MIN_FALLBACK_CHARS


def test_a_javascript_shell_is_a_fetch_error(monkeypatch):
    item = _fetch(monkeypatch, SHELL)

    assert item.content is None
    assert item.extraction_method is None
    assert "may need JavaScript" in (item.fetch_error or "")


def test_a_shell_is_attempted_but_not_counted_ok(monkeypatch):
    item = _fetch(monkeypatch, SHELL)
    run = SearchRun("q", [item], [])

    _stamp_fetch_stats(run, requested=True)

    assert (run.fetch_attempted, run.fetch_ok, run.fetch_failed) == (1, 0, 1)


def test_fallback_text_of_the_minimum_length_is_returned(monkeypatch):
    words = " ".join(["word"] * (MIN_FALLBACK_CHARS // 5 + 1))
    html = f"<html><head><title>t</title></head><body><div>{words}</div></body></html>"
    assert extract(html, "https://help.example.org/page")[1] == "html-text-fallback"

    item = _fetch(monkeypatch, html)

    assert item.fetch_error is None
    assert item.content and len(item.content) >= MIN_FALLBACK_CHARS


@pytest.mark.parametrize("body", ["<h1>Doc</h1><p>Text</p>", "<p>ok</p>"])
def test_a_short_trafilatura_result_is_never_rejected(monkeypatch, body):
    monkeypatch.setattr("josty.engine.extract", lambda html, url: ("short", "trafilatura"))
    item = _fetch(monkeypatch, f"<html><body>{body}</body></html>")

    assert item.content == "short"
    assert item.fetch_error is None
