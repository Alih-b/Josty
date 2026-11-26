"""Suite-wide isolation for the keyless sources Josty calls without ``ddgs``.

AGENTS.md invariant 2: no test may touch the network. The ``ddgs`` engines are
stubbed per test by patching ``josty.engine.DDGS``. ``mwmbl`` and ``wikipedia`` are
native HTTP adapters, so their one wire call is stubbed once, here. A test that
wants real Mwmbl behavior patches :meth:`josty.providers.MwmblSearchAdapter._request`
itself (see ``tests/test_mwmbl.py``); an autouse patch of that method is safe to
override with ``monkeypatch`` inside the test body.
"""

from __future__ import annotations

import pytest

from josty.providers import MwmblSearchAdapter, WikipediaSearchAdapter


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path, monkeypatch):
    """Every test gets its own cache DB.

    Since #105 the breaker remembers open circuits there: a trip in one test
    must not suspend an engine in another, and no test may touch the user's.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _stub_mwmbl_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every Mwmbl call locally with an empty result set."""

    async def empty_request(
        self: MwmblSearchAdapter, client: object, query: str
    ) -> dict:
        return {"results": []}

    monkeypatch.setattr(MwmblSearchAdapter, "_request", empty_request)


@pytest.fixture(autouse=True)
def _stub_wikipedia_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every Wikipedia call locally with a certified empty page (#102)."""

    async def empty_request(
        self: WikipediaSearchAdapter, client: object, query: str, limit: int
    ) -> dict:
        return {"query": {"searchinfo": {"totalhits": 0}, "search": []}}

    monkeypatch.setattr(WikipediaSearchAdapter, "_request", empty_request)


@pytest.fixture
def ddgs_google_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable Google in the ddgs text registry for one test.

    josty follows the installed ddgs registry, and upstream disables Google in
    some releases. A test that fans out to Google opts in here instead of
    depending on which ddgs is installed.
    """
    from ddgs.engines import ENGINES
    from ddgs.engines.google import Google

    monkeypatch.setitem(ENGINES["text"], "google", Google)
