"""The offline status matrix (#103): a stubbed wire, and the kind Josty must report.

Live yield is egress noise; whether a status is true is not. Each row below is
one (stubbed wire, outcome) pair pushed through the real branch pipeline -- the
same path a live call takes (_finish, _classify_search_error, ProviderStatus) --
with no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from ddgs.exceptions import DDGSException, TimeoutException

from josty.branch import BranchRunner, IssueContext, IssueOutcome, SearchCall
from josty.breaker import CircuitBreaker
from josty.models import ProviderStatus, SearchResult, WireFacts, WireResponse

NO_RESULT = "No results found."


@dataclass(frozen=True)
class Case:
    """One row: what the wire said, what ddgs raised, what Josty must answer."""

    name: str
    responses: tuple[tuple[int, int, bool], ...] = ()
    error: Exception | None = None
    rows: int = 0
    host: str = "search.example.com"
    resolved: tuple[str, ...] = ()
    expect_ok: bool = False
    expect_kind: str | None = None


SMALL_PAGE = (200, 2048, False)
LARGE_PAGE = (200, 145218, False)  # Yahoo's second markup variant, #100
BLOCK_PAGE = (200, 86370, True)  # "Startpage Blocked" over a VPN, 86 KB
CAPTCHA_PAGE = (200, 5505, True)  # a captcha title, the mojeek capture

def _refused(name: str, status: int, kind: str) -> Case:
    """A zero-row call whose only response was a non-200."""
    return Case(name, ((status, 0, False),), DDGSException(NO_RESULT), expect_kind=kind)


MATRIX: tuple[Case, ...] = (
    Case("200_with_rows", ((200, 40000, False),), rows=10, expect_ok=True),
    Case(
        "200_zero_rows_small_page",
        (SMALL_PAGE,),
        DDGSException(NO_RESULT),
        expect_ok=True,
        expect_kind="empty",
    ),
    Case(
        "200_zero_rows_large_unparsed",
        (LARGE_PAGE,),
        DDGSException(NO_RESULT),
        expect_kind="parse",
    ),
    Case(
        "200_zero_rows_block_page",
        (BLOCK_PAGE,),
        DDGSException(NO_RESULT),
        expect_kind="blocked",
    ),
    Case(
        "200_zero_rows_captcha_page",
        (CAPTCHA_PAGE,),
        DDGSException(NO_RESULT),
        expect_kind="blocked",
    ),
    _refused("202_challenge_status", 202, "blocked"),
    _refused("403_forbidden", 403, "blocked"),
    _refused("429_rate_limited", 429, "rate_limited"),
    _refused("503_unavailable", 503, "blocked"),
    _refused("500_server_error", 500, "network"),
    _refused("404_endpoint_moved", 404, "parse"),
    Case("timeout", (), TimeoutException("timed out"), expect_kind="network"),
    Case(
        # Iran-direct: no HTTP response, and the host resolves into the filter.
        "sinkholed_resolve",
        (),
        DDGSException("ConnectionError: SSL handshake failed"),
        host="www.startpage.com",
        resolved=("10.10.34.36",),
        expect_kind="filtered",
    ),
    Case(
        # Not filtered: the path to the host is an ordinary one.
        "ordinary_address_no_response",
        (),
        DDGSException("ConnectionError: connection reset by peer"),
        resolved=("93.184.216.34",),
        expect_kind="network",
    ),
    Case(
        # A 403 is a country decision only for a host a two-egress probe proved.
        "geo_403",
        ((403, 512, False),),
        DDGSException(NO_RESULT),
        host="hn.algolia.com",
        expect_kind="geo",
    ),
    Case(
        # A hit whose sub-requests were 30% refused stays a hit, with the rate.
        "bing_like_10_rows_30pct_refused",
        ((200, 4000, False),) * 7 + ((503, 120, False),) * 3,
        rows=10,
        expect_ok=True,
    ),
)


def facts_of(case: Case) -> WireFacts:
    return WireFacts(
        [WireResponse(status, size, challenged) for status, size, challenged in case.responses],
        host=case.host,
        resolved=case.resolved,
    )


async def _status_for(case: Case) -> ProviderStatus:
    """Run one row through the real branch pipeline with a stubbed adapter."""

    class Stub:
        provider = "matrix"

        def precheck(self, call: SearchCall) -> str | None:
            return None

        async def issue(self, call: SearchCall, ctx: IssueContext) -> IssueOutcome:
            rows = [
                SearchResult("t", f"https://example.org/{index}", "s", sources=["matrix"])
                for index in range(case.rows)
            ]
            return IssueOutcome(rows, 1.0, case.error, wire=facts_of(case))

    runner = BranchRunner.unadmitted(
        breaker=CircuitBreaker(), semaphore=asyncio.Semaphore(1), timeout=1.0, headroom=0.0
    )
    call = SearchCall("q", "matrix", 10, "text", None, "moderate", None)
    _, status = await runner.run(call, Stub())
    return status


@pytest.mark.parametrize("case", MATRIX, ids=lambda case: case.name)
def test_case(case: Case) -> None:
    status = asyncio.run(_status_for(case))

    assert status.ok is case.expect_ok, status.error
    assert status.error_kind == case.expect_kind, status.error


def test_a_hit_with_30pct_refused_sub_requests_is_not_a_clean_success() -> None:
    """#103: ten results, three of ten sub-requests refused: the rate must travel."""
    status = asyncio.run(_status_for(MATRIX[-1]))

    assert status.ok is True
    assert status.result_count == 10
    assert status.refusal_rate == 0.3
    assert status.http_status == 503
    # It answered: the refusals are telemetry on a hit, not a failure.
    assert status.error_kind is None
