"""Rows that came back without a link are `parse`, not `empty` (#131).

The item markup is cut from a live 200 response of 70 KB: the result link wraps
the h2 inside div.b_algoheader, where ddgs looks for h2 > a, so ddgs returns the
snippet with an empty title and href. Hermetic: the
engine's socket is stubbed at the primp client, under the tap (AGENTS.md
invariant 2).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from josty.engine import Josty

ITEM = (
    '<li class="b_algo" data-id iid="SERP.{n}"><div class="b_algoheader">'
    '<a href="https://example.org/{n}" h="ID=SERP,5108.2"><h2><strong>Result</strong> {n}</h2>'
    '</a></div><div class="b_caption"><p class="b_lineclamp3">Snippet {n}.</p></div></li>'
)


def _page(items: int, padding: int = 0) -> bytes:
    body = "".join(ITEM.format(n=n) for n in range(items))
    return (
        "<html><head><title>q - Search</title></head><body><ol id=\"b_results\">"
        f"{body}</ol>{'<p>filler</p>' * padding}</body></html>"
    ).encode()


def _wire(body: bytes):
    def factory(**kwargs):
        from ddgs import DDGS

        ddgs = DDGS(timeout=5)
        for engine in ddgs._get_engines("text", "bing"):
            engine.http_client.client = SimpleNamespace(
                request=lambda *a, **k: SimpleNamespace(
                    status_code=200, content=body, text=body.decode("utf-8")
                ),
                set_cookies=lambda *a, **k: None,
            )
        return ddgs

    return factory


def _provider(monkeypatch, body: bytes):
    monkeypatch.setattr("josty.engine.DDGS", _wire(body))
    run = asyncio.run(Josty(backends=("bing",), enable_cache=False).search_run("q", limit=5))
    return run.providers[0]


def test_a_small_page_with_result_items_and_no_rows_is_parse(monkeypatch):
    body = _page(5)
    assert len(body) < 100_000

    provider = _provider(monkeypatch, body)

    assert provider.http_status == 200
    assert provider.error_kind == "parse"
    assert provider.ok is False


def test_a_small_page_without_result_items_is_still_empty(monkeypatch):
    body = _page(0)

    provider = _provider(monkeypatch, body)

    assert provider.error_kind == "empty"
    assert provider.ok is True


def test_a_page_ddgs_rejects_outright_keeps_its_own_classification(monkeypatch):
    body = _page(0, padding=9000)
    assert len(body) > 100_000

    assert _provider(monkeypatch, body).error_kind == "parse"


def test_ad_only_rows_are_not_a_parse_miss(monkeypatch):
    """Rows that carry a link, even an ad redirect that is dropped, were read."""

    class Ads:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            return [{"title": "Ad", "href": "https://www.bing.com/ck/a?x=1", "body": "buy"}]

    monkeypatch.setattr("josty.engine.DDGS", Ads)
    run = asyncio.run(Josty(backends=("bing",), enable_cache=False).search_run("q", limit=5))

    assert run.providers[0].error_kind == "empty"


def test_rows_without_a_link_are_parse_for_a_stubbed_engine(monkeypatch):
    class Linkless:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            return [{"title": "", "href": "", "body": "Snippet"}] * 3

    monkeypatch.setattr("josty.engine.DDGS", Linkless)
    run = asyncio.run(Josty(backends=("bing",), enable_cache=False).search_run("q", limit=5))

    assert run.providers[0].error_kind == "parse"
    assert "3 rows came back without a link" in run.providers[0].error
