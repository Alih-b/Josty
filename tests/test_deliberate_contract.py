"""Pins for the deliberate behaviour changes that landed with the fanout refactor.

These six changes are deliberate, were found by comparing the base revision
against the refactor, and are the only user-visible behaviour differences in the
change set. The fourth, the admission order under saturation, has its *outcome*
pinned here; the ordering itself is internal and pinned at the seam instead
(``SEAM_PINNED_ELSEWHERE``). The last two are consequences of routing GitHub
through the shared branch pipeline instead of a second code path.

This module is written against the **public** surface only (``josty.cli.main`` and
``Josty.research_run``) on purpose: the identical file can be dropped into a
worktree of the base revision and run there. That is the whole point. A test that
only ever runs at HEAD proves the code agrees with itself; the red half is what
separates a deliberate change from a test written to match whatever the code now
does::

    git worktree add /tmp/josty-base e886eff
    cp tests/test_deliberate_contract.py /tmp/josty-base/tests/
    (cd /tmp/josty-base && pytest -q tests/test_deliberate_contract.py)  # 9 failed
    pytest -q tests/test_deliberate_contract.py                          # 9 passed

Hermetic: fake ddgs, fake HTTP, patched registry — no network (invariant 2).
"""

from __future__ import annotations

import asyncio
import io
import json
import time

import httpx
import pytest

import josty.engine as eng
from josty import Josty
from josty.cli import main

#: One row per deliberate change: id -> (claim, base revision behaviour,
#: refactor behaviour, the pin that asserts it).
DELIBERATE = {
    "github-zero-item-is-empty": (
        "a GitHub 200 with no items reports error_kind='empty', not null",
        "ok=True, result_count=0, error_kind=None",
        'ok=True, result_count=0, error_kind="empty"',
        "test_github_zero_item_reports_empty",
    ),
    "stdin-results-must-be-a-list": (
        "fetch --stdin with a non-list 'results' is a usage error, not a crash",
        "TypeError, exit 1, traceback on stderr",
        "exit 2, JSON error on stderr, stdout empty",
        "test_stdin_non_list_results_exits_2",
    ),
    "stdin-json-array-is-parsed": (
        "fetch --stdin accepts a bare JSON array as result rows",
        "array line-split into fake URLs, exit 0",
        "array parsed, real rows returned, exit 0",
        "test_stdin_json_array_is_parsed",
    ),
    "admission-order-under-saturation": (
        "under saturation every branch is admitted: complete, 7 issued, 0 shed",
        "degraded, 6 issued, 1 shed (capacity)",
        "complete, 7 issued, 0 shed",
        "test_busy_pool_admits_every_branch",
    ),
    "github-result-count-is-distinct-urls": (
        "github-api result_count counts distinct canonical URLs, not rows returned",
        "result_count=2 for a reply with the same repo twice",
        "result_count=1 for the same reply",
        "test_github_result_count_is_distinct_canonical_urls",
    ),
    "github-empty-does-not-clear-the-breaker": (
        "a zero-item GitHub 200 keeps the breaker's failure history",
        "record_success on any 200: primed failures 2 -> 0",
        "empty-ok carve-out: primed failures stay 2, error_kind='empty'",
        "test_empty_github_200_does_not_clear_the_breaker",
    ),
}

#: The fourth deliberate change (a branch takes its concurrency slot before its
#: lease, so GitHub stops shedding a web engine under saturation) is pinned at the
#: seam, in tests/test_fanout_leases.py, because that file imports modules the base
#: revision does not have.
SEAM_PINNED_ELSEWHERE = ("test_slot_is_taken_before_a_lease_no_branch_holds_a_lease_while_parked",)


class _OneRowDDGS:
    """The smallest fake backend that yields a live engine row."""

    def __init__(self, timeout=None):
        self.timeout = timeout

    def text(self, query, backend=None, **kwargs):
        return [{"title": "t", "href": "https://example.org/x", "body": "b"}]

    news = text


class _EmptyGithubResponse:
    def __init__(self) -> None:
        self.status_code = 200
        self.request = httpx.Request("GET", "https://api.github.com/search/repositories")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"items": []}


def test_github_zero_item_reports_empty(monkeypatch):
    """DELIBERATE: github-zero-item-is-empty."""
    monkeypatch.setattr(eng, "DDGS", _OneRowDDGS)
    monkeypatch.setattr(eng, "_engine_available", lambda category, backend: (True, None))

    async def empty_github(self, url, **kwargs):
        return _EmptyGithubResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", empty_github)

    engine = Josty(backends=("brave",), enable_cache=False)
    run = asyncio.run(engine.research_run("q", limit=5, include_github=True))
    row = next(p for p in run.providers if p.provider == "github-api")

    assert row.ok is True
    assert row.result_count == 0
    assert row.error_kind == "empty"


@pytest.mark.parametrize(
    "payload", ['{"results": 5}', '{"results": null}', '{"results": {"a": 1}}']
)
def test_stdin_non_list_results_exits_2(monkeypatch, capsys, payload):
    """DELIBERATE: stdin-results-must-be-a-list.

    Covers with and without --limit: the guard must sit before both the slice and
    the iteration, or one of the two paths still raises TypeError.
    """
    for argv in (["fetch", "--stdin"], ["fetch", "--stdin", "--limit", "2"]):
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        captured = capsys.readouterr()
        assert excinfo.value.code == 2, f"{payload} {argv}: exit {excinfo.value.code}"
        assert captured.out == ""
        assert json.loads(captured.err)["error"]


def test_stdin_json_array_is_parsed(monkeypatch, capsys):
    """DELIBERATE: stdin-json-array-is-parsed."""
    fetched: list[str] = []

    async def fake_fetch_content(self, results):
        for item in results:
            fetched.append(item.url)
            item.content = f"content of {item.url}"

    monkeypatch.setattr(Josty, "fetch_content", fake_fetch_content)
    payload = json.dumps(
        [
            {"title": "a", "url": "https://example.org/a"},
            {"title": "b", "url": "https://example.org/b"},
            {"title": "no url"},
        ]
    )

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    main(["fetch", "--stdin", "--limit", "2"])
    rows = json.loads(capsys.readouterr().out)

    assert [row["url"] for row in rows] == ["https://example.org/a", "https://example.org/b"]
    assert set(rows[0]) == {
        "url",
        "content",
        "extraction_method",
        "fetched_url",
        "fetched_at",
        "fetch_error",
    }
    assert fetched == ["https://example.org/a", "https://example.org/b"]

    monkeypatch.setattr("sys.stdin", io.StringIO("[]"))
    main(["fetch", "--stdin"])
    assert json.loads(capsys.readouterr().out) == []


class _SaturatingDDGS:
    """Six web engines that each hold their lease for a real interval.

    The slowness is the point: with instant engines every lease is released
    before GitHub asks for one, saturation never happens, and both revisions
    admit all seven branches.
    """

    def __init__(self, timeout=None):
        self.timeout = timeout

    def text(self, query, backend=None, **kwargs):
        time.sleep(0.25)
        return [
            {
                "title": f"slow {backend}",
                "href": f"https://example.org/slow-{backend}",
                "body": "b",
            }
        ]

    news = text


class _TwoItemGithubResponse:
    def __init__(self) -> None:
        self.status_code = 200
        self.request = httpx.Request("GET", "https://api.github.com/search/repositories")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
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


def test_busy_pool_admits_every_branch(monkeypatch):
    """DELIBERATE: admission-order-under-saturation.

    Six engines answer in 250ms and the GitHub reply takes 300ms, so all seven
    branches want admission at once against a six-slot pool. In the base revision
    GitHub takes its lease *before* waiting for a concurrency slot, so it holds
    capacity a web engine needed: 6 issued, 1 shed, ``degraded``. Here every
    branch takes its slot first and only then its lease: 7 issued, 0 shed,
    ``complete``. The ordering test in ``test_fanout_leases`` watches the order
    itself; this one pins the outcome the change set claims for it.
    """
    monkeypatch.setattr(eng, "DDGS", _SaturatingDDGS)
    monkeypatch.setattr(eng, "_engine_available", lambda category, backend: (True, None))

    async def slow_github(self, url, **kwargs):
        # GitHub must hold its own slot for a real interval too: an instant reply
        # frees a slot before the engines are parked, which hides the difference.
        await asyncio.sleep(0.3)
        return _TwoItemGithubResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", slow_github)

    engine = Josty(
        backends=("brave,duckduckgo", "google,mojeek,startpage", "yahoo"),
        enable_cache=False,
    )
    run = asyncio.run(engine.research_run("busy pool", limit=5, include_github=True))

    assert run.status == "complete", [p.error for p in run.providers if not p.ok]
    assert run.request_count == 7
    assert run.shed_count == 0


class _DuplicateRowGithubResponse:
    """The same repository twice: identical ``html_url``, differing description."""

    def __init__(self) -> None:
        self.status_code = 200
        self.request = httpx.Request("GET", "https://api.github.com/search/repositories")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "items": [
                {"full_name": "o/r", "html_url": "https://github.com/o/r", "description": "a"},
                {"full_name": "o/r", "html_url": "https://github.com/o/r", "description": "b"},
            ]
        }


def test_github_result_count_is_distinct_canonical_urls(monkeypatch):
    """DELIBERATE: github-result-count-is-distinct-urls.

    GitHub now reports through the same per-engine aggregation as every web
    engine, so ``result_count`` is a count of distinct canonical URLs rather than
    of rows the provider returned. The base revision counted rows (2 here).
    """
    monkeypatch.setattr(eng, "DDGS", _OneRowDDGS)
    monkeypatch.setattr(eng, "_engine_available", lambda category, backend: (True, None))

    async def duplicate_github(self, url, **kwargs):
        return _DuplicateRowGithubResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", duplicate_github)

    engine = Josty(backends=("brave",), enable_cache=False)
    run = asyncio.run(engine.research_run("dup gh", limit=5, include_github=True))
    row = next(p for p in run.providers if p.provider == "github-api")

    assert row.result_count == 1


def test_empty_github_200_does_not_clear_the_breaker(monkeypatch):
    """DELIBERATE: github-empty-does-not-clear-the-breaker.

    The empty-ok carve-out exists once, so it now covers GitHub too: a 200 that
    returned nothing is a successful-empty branch that must not reset rate-limit
    history. The base revision called ``record_success`` on any 200, so a primed
    breaker came back with zero failures.
    """
    monkeypatch.setattr(eng, "DDGS", _OneRowDDGS)
    monkeypatch.setattr(eng, "_engine_available", lambda category, backend: (True, None))

    async def empty_github(self, url, **kwargs):
        return _EmptyGithubResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", empty_github)

    engine = Josty(backends=("brave",), enable_cache=False, breaker_fail_threshold=100)
    for _ in range(2):
        engine.breaker.record_failure("github-api", "search")

    run = asyncio.run(engine.research_run("empty gh", limit=5, include_github=True))
    row = next(p for p in run.providers if p.provider == "github-api")

    assert row.failures == 2, "a zero-result 200 cleared the breaker"
    assert row.error_kind == "empty"


def test_every_deliberate_change_has_a_live_pin():
    """A deliberate change cannot be reverted by deleting its pin quietly.

    Every ledger entry and the seam-pinned change must still be backed by a test
    function that exists and is neither skipped nor expected to fail.
    """
    import test_fanout_leases  # noqa: PLC0415 - resolved via pytest's test path

    for pin in SEAM_PINNED_ELSEWHERE:
        assert hasattr(test_fanout_leases, pin), f"seam pin missing: {pin}"

    for change_id, (_, _, _, pin_name) in DELIBERATE.items():
        function = globals().get(pin_name)
        assert callable(function), f"no live pin for {change_id}: {pin_name}"
        markers = {mark.name for mark in getattr(function, "pytestmark", [])}
        assert not markers & {"skip", "xfail"}, f"{change_id} is skipped: {markers}"
