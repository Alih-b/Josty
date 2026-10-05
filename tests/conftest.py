"""Suite-wide isolation for the keyless sources Josty calls without ``ddgs``.

AGENTS.md invariant 2: no test may touch the network. The ``ddgs`` engines are
stubbed per test by patching ``josty.engine.DDGS``. ``mwmbl`` is a native HTTP
adapter, so its one wire call is stubbed once, here. A test that wants real
Mwmbl behavior patches :meth:`josty.providers.MwmblSearchAdapter._request`
itself (see ``tests/test_mwmbl.py``); an autouse patch of that method is safe to
override with ``monkeypatch`` inside the test body.
"""

from __future__ import annotations

import pytest

from josty.providers import MwmblSearchAdapter


@pytest.fixture(autouse=True)
def _stub_mwmbl_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every Mwmbl call locally with an empty result set."""

    async def empty_request(
        self: MwmblSearchAdapter, client: object, query: str
    ) -> dict:
        return {"results": []}

    monkeypatch.setattr(MwmblSearchAdapter, "_request", empty_request)
