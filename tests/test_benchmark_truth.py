"""The live status-truth capture's own bookkeeping (#103), run offline."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from josty.models import ProviderStatus, SearchRun


def _benchmark(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["benchmark.py"])
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    return importlib.import_module("benchmark")


def test_a_subdomain_capture_is_judged_for_its_engine(monkeypatch):
    """ddgs's DuckDuckGo text engine asks html.duckduckgo.com, not duckduckgo.com."""
    benchmark = _benchmark(monkeypatch)
    status = ProviderStatus("duckduckgo", "q", True, 0, error_kind="empty")
    seen = [
        {
            "host": "html.duckduckgo.com",
            "http_status": 200,
            "body_bytes": 1200,
            "page_title": None,
            "challenge": False,
        }
    ]

    truth = benchmark.truth_of(SearchRun("q", results=[], providers=[status]), seen)

    assert truth["judged"] == 1
    assert truth["providers"][0]["http_statuses_all"] == [200]
