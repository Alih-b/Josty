"""Behavior-preservation probe for the fanout/branch refactor (hermetic; no network).

Runs a fixed set of engine scenarios against a fake DDGS and writes a canonical
JSON snapshot. Run it on the base revision and on the refactor, then diff the two
snapshots with ``compare.py``: structure may move, behavior must not. This is the
repeatable form of the change set's "Nothing else moved" claim; see README.md.

Usage:  .venv/bin/python scripts/refactor_probe/envelope_probe.py <out.json>
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

import httpx  # noqa: E402
from mock_ddgs import MockDDGSEngine  # noqa: E402

import josty.engine as engine_mod  # noqa: E402
from josty import Josty  # noqa: E402

ORIG_DDGS = engine_mod.DDGS


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.request = httpx.Request("GET", "https://api.github.com/search/repositories")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


GITHUB_ITEMS = {
    "items": [
        {
            "full_name": "owner/repo-one",
            "html_url": "https://github.com/owner/repo-one",
            "description": "first repo",
        },
        {
            "full_name": "owner/repo-two",
            "html_url": "https://github.com/owner/repo-two",
            "description": "second repo",
        },
    ]
}


GITHUB_ITEMS_EMPTY = {"items": []}


def canonical(run) -> dict:
    payload = run.dict()
    payload.pop("run_at", None)
    return payload


async def scenario_happy() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    try:
        engine = Josty(backends=("brave,duckduckgo", "yahoo"), enable_cache=False)
        run = await engine.research_run("probe happy", limit=5)
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_one_engine_fails() -> dict:
    engine_mod.DDGS = MockDDGSEngine({"duckduckgo": ConnectionError("beta is down")})
    try:
        engine = Josty(backends=("brave,duckduckgo", "yahoo"), enable_cache=False)
        run = await engine.research_run("probe degraded", limit=5)
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_all_fail() -> dict:
    engine_mod.DDGS = MockDDGSEngine(
        {
            "brave": ConnectionError("alpha down"),
            "duckduckgo": ConnectionError("beta down"),
            "yahoo": ConnectionError("gamma down"),
        }
    )
    try:
        engine = Josty(backends=("brave", "duckduckgo", "yahoo"), enable_cache=False)
        run = await engine.research_run("probe failed", limit=5)
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_empty() -> dict:
    engine_mod.DDGS = MockDDGSEngine({"brave": [], "duckduckgo": [], "yahoo": []})
    try:
        engine = Josty(backends=("brave,duckduckgo", "yahoo"), enable_cache=False)
        run = await engine.research_run("probe empty", limit=5)
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_news() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    try:
        engine = Josty(
            backends=("brave,duckduckgo",),
            news_backends=("yahoo",),
            enable_cache=False,
        )
        run = await engine.search_run("probe news", limit=5, category="news")
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_sites_and_variants() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    try:
        engine = Josty(backends=("brave,duckduckgo",), enable_cache=False)
        run = await engine.research_run(
            "probe variants",
            limit=5,
            mode="oss",
            max_query_variants=2,
            sites=["example.org"],
        )
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_deadline_shed() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    try:
        engine = Josty(backends=("brave,duckduckgo", "yahoo"), enable_cache=False, run_timeout=1e-9)
        run = await engine.research_run("probe shed", limit=5)
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_breaker_open() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    try:
        engine = Josty(backends=("brave,duckduckgo",), enable_cache=False)
        for _ in range(3):
            engine.breaker.record_failure("brave", "search")
            engine.breaker.record_failure("duckduckgo", "search")
        run = await engine.research_run("probe breaker", limit=5)
        return canonical(run)
    finally:
        engine_mod.DDGS = ORIG_DDGS


async def scenario_github() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    original_get = httpx.AsyncClient.get

    async def fake_get(self, url, **kwargs):
        return FakeResponse(GITHUB_ITEMS)

    httpx.AsyncClient.get = fake_get
    try:
        engine = Josty(backends=("brave",), enable_cache=False)
        run = await engine.research_run("probe github", limit=5, include_github=True)
        return canonical(run)
    finally:
        httpx.AsyncClient.get = original_get
        engine_mod.DDGS = ORIG_DDGS


async def scenario_github_saturated() -> dict:
    """6 slow web engines + GitHub against a 6-slot pool: pins admission.

    The slowness is the point: with instant mock engines every lease is released
    before the GitHub branch asks for one, so both revisions admit it. Holding the
    six leases for a real interval reproduces saturation, which is the only regime
    where 'GitHub contends for a lease immediately' and 'GitHub waits for the
    shared semaphore' differ.
    """
    def slow_response(query, **kwargs):
        time.sleep(0.25)
        backend = kwargs.get("backend", "duckduckgo")
        return [
            {
                "title": f"Slow result from {backend}",
                "href": f"https://example.org/slow-{backend}",
                "body": f"Slow snippet for {backend}",
                "source": backend,
            }
        ]

    names = ("brave", "duckduckgo", "google", "mojeek", "startpage", "yahoo")
    engine_mod.DDGS = MockDDGSEngine({name: slow_response for name in names})
    original_get = httpx.AsyncClient.get

    async def fake_get(self, url, **kwargs):
        # GitHub must also occupy its lease for a real interval: an instant reply
        # releases it immediately and frees a slot, which hides the difference.
        await asyncio.sleep(0.3)
        return FakeResponse(GITHUB_ITEMS)

    httpx.AsyncClient.get = fake_get
    try:
        engine = Josty(backends=("brave,duckduckgo", "google,mojeek,startpage", "yahoo"),
                       enable_cache=False)
        run = await engine.research_run("probe github saturated", limit=5, include_github=True)
        return canonical(run)
    finally:
        httpx.AsyncClient.get = original_get
        engine_mod.DDGS = ORIG_DDGS


async def scenario_github_fails() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    original_get = httpx.AsyncClient.get

    async def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("github unreachable")

    httpx.AsyncClient.get = fake_get
    try:
        engine = Josty(backends=("brave",), enable_cache=False)
        run = await engine.research_run("probe github down", limit=5, include_github=True)
        return canonical(run)
    finally:
        httpx.AsyncClient.get = original_get
        engine_mod.DDGS = ORIG_DDGS


async def scenario_github_empty() -> dict:
    """A GitHub 200 that returns zero items: pins the provider-row shape."""
    engine_mod.DDGS = MockDDGSEngine()
    original_get = httpx.AsyncClient.get

    async def fake_get(self, url, **kwargs):
        return FakeResponse(GITHUB_ITEMS_EMPTY)

    httpx.AsyncClient.get = fake_get
    try:
        engine = Josty(backends=("brave",), enable_cache=False)
        run = await engine.research_run("probe github empty", limit=5, include_github=True)
        return canonical(run)
    finally:
        httpx.AsyncClient.get = original_get
        engine_mod.DDGS = ORIG_DDGS


async def scenario_cache_roundtrip() -> dict:
    engine_mod.DDGS = MockDDGSEngine()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            engine = Josty(backends=("brave",), cache_db=Path(tmp) / "c.db")
            first = await engine.research_run("probe cache", limit=5)
            second = await engine.research_run("probe cache", limit=5)
            return {
                "first": canonical(first),
                "second": canonical(second),
                "stats": engine.cache_stats(),
            }
    finally:
        engine_mod.DDGS = ORIG_DDGS


SCENARIOS = {
    "happy": scenario_happy,
    "one_engine_fails": scenario_one_engine_fails,
    "all_fail": scenario_all_fail,
    "empty": scenario_empty,
    "news": scenario_news,
    "sites_and_variants": scenario_sites_and_variants,
    "deadline_shed": scenario_deadline_shed,
    "breaker_open": scenario_breaker_open,
    "github": scenario_github,
    "github_saturated": scenario_github_saturated,
    "github_empty": scenario_github_empty,
    "github_fails": scenario_github_fails,
    "cache_roundtrip": scenario_cache_roundtrip,
}


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("envelope_probe.json")
    snapshot: dict = {}
    for name, scenario in SCENARIOS.items():
        try:
            snapshot[name] = asyncio.run(scenario())
        except Exception as exc:  # a raised exception is itself behavior worth pinning
            snapshot[name] = {"__raised__": f"{type(exc).__name__}: {exc}"}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n")
    print(f"wrote {out_path} ({len(snapshot)} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
