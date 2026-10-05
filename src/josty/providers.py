"""Provider adapters: the only place one upstream's wire call lives.

An adapter answers exactly two questions -- "may this call be attempted?" and
"perform it, counting it at the issue site" -- and knows nothing about gating,
admission, classification, or telemetry. :class:`~josty.branch.BranchRunner`
owns all of that, so the ddgs engine call and the GitHub REST call share one
policy instead of two copies of it.

``ledger.record()`` sits immediately before each network call, never earlier:
the ledger measures calls issued, not tasks scheduled (AGENTS.md invariant 4).
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from .branch import IssueContext, IssueOutcome, SearchCall
from .fetch import is_ad_redirect
from .models import SearchResult
from .status import SearchCategory

if TYPE_CHECKING:
    from .fanout import _FanoutLedger
    from .lease import LeasePool


class DdgsSearchAdapter:
    """The blocking ddgs engine call, wrapped for the branch pipeline."""

    provider: str

    def __init__(
        self,
        *,
        provider: str,
        client_factory: Callable[..., Any],
        available: Callable[[SearchCategory, str], tuple[bool, str | None]],
        timeout: float,
    ) -> None:
        self.provider = provider
        self._client_factory = client_factory
        self._available = available
        self._timeout = timeout

    def precheck(self, call: SearchCall) -> str | None:
        """Skip reason from the ddgs registry, or None to proceed."""
        available, message = self._available(call.category, self.provider)
        if available:
            return None
        return message

    async def issue(self, call: SearchCall, ctx: IssueContext) -> IssueOutcome:
        lease_id = ctx.lease.lease_id if ctx.lease is not None else None
        t_start = time.perf_counter()
        try:
            results, latency_ms, exc = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    ctx.executor,
                    functools.partial(
                        self._worker,
                        call,
                        ledger=ctx.ledger,
                        pool=ctx.pool,
                        lease_id=lease_id,
                    ),
                ),
                timeout=ctx.budget,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # The worker keeps running (a "ghost"): a blocked socket call
            # cannot be cancelled, so this branch reports the timeout now and
            # the thread ends on ddgs's own client timeout. Nothing reads its
            # return value, so a late success cannot revive the branch. The
            # lease is retained here -- the ghost releases it in its own
            # finally, and that release is idempotent.
            latency_ms = round((time.perf_counter() - t_start) * 1000, 2)
            return IssueOutcome([], latency_ms, exc, lease_retained=True)
        return IssueOutcome(results, latency_ms, exc)

    def _worker(
        self,
        call: SearchCall,
        *,
        ledger: _FanoutLedger,
        pool: LeasePool | None,
        lease_id: int | None,
    ) -> tuple[list[SearchResult], float, Exception | None]:
        """One blocking engine call, run on a worker thread.

        Always retires its own lease, including on the exception path. The release
        is idempotent, so returning *after* the arbiter already reaped this lease
        as a ghost is a safe no-op rather than a double free.
        """
        t0 = time.perf_counter()
        try:
            # A fresh DDGS client per call is deliberate, not waste:
            # ddgs engine instances carry a shared cached_property lxml
            # parser, which is not thread-safe. Caching one client per
            # backend would let concurrent query variants of the same
            # engine parse HTML on one parser — the same C-level
            # corruption class already fixed for trafilatura extraction.
            ddgs = self._client_factory(timeout=self._timeout)
            method = ddgs.news if call.category == "news" else ddgs.text
            kwargs: dict[str, Any] = {
                "backend": call.backend,
                "max_results": call.limit,
                "safesearch": call.safesearch,
            }
            if call.region:
                kwargs["region"] = call.region
            if call.timelimit:
                kwargs["timelimit"] = call.timelimit
            # The one request site: counted immediately before the call so the
            # ledger measures calls issued, not tasks scheduled.
            ledger.record()
            rows = method(call.query, **kwargs)
            results = []
            rank = 1
            for row in rows:
                result_url = row.get("href") or row.get("url") or ""
                if result_url and not is_ad_redirect(result_url):
                    results.append(
                        SearchResult(
                            title=row.get("title", ""),
                            url=result_url,
                            snippet=row.get("body", ""),
                            sources=[call.backend],
                            published_at=row.get("date"),
                            publisher=row.get("source"),
                            engine_ranks={call.backend: rank},
                        )
                    )
                    rank += 1
            return results, round((time.perf_counter() - t0) * 1000, 2), None
        except Exception as exc:
            return [], round((time.perf_counter() - t0) * 1000, 2), exc
        finally:
            if pool is not None and lease_id is not None:
                pool.release(lease_id)


#: Mwmbl's public search API: keyless JSON, one request returns the whole
#: result set (it ignores limit/offset parameters), so a run makes exactly one
#: call per query variant.
MWMBL_SEARCH_URL = "https://api.mwmbl.org/api/v2/search/"


class MwmblSearchAdapter:
    """Mwmbl's keyless JSON web-search API.

    A source Josty calls itself rather than through ``ddgs``, because ``ddgs``
    has no Mwmbl engine. The API takes only a query: it has no date, region or
    safe-search parameter, so :meth:`precheck` refuses a call whose filter it
    cannot honour rather than returning results that ignore the caller.
    """

    provider: str = "mwmbl"

    def __init__(self, *, timeout: float, user_agent: str) -> None:
        self._timeout = timeout
        self._user_agent = user_agent

    def precheck(self, call: SearchCall) -> str | None:
        """Refuse a call whose filter this API cannot honour, or None to proceed."""
        if call.category != "text":
            return "skipped: mwmbl serves web search only, not news"
        if call.timelimit is not None:
            return "skipped: mwmbl does not support time-limited search"
        if call.region is not None:
            return "skipped: mwmbl does not support region selection"
        if call.safesearch == "on":
            # "on" is a strict request. At the permissive "moderate" default this
            # source is no different from the installed duckduckgo, yahoo,
            # grokipedia and wikipedia engines, which take no safe-search
            # parameter either and are queried anyway.
            return "skipped: mwmbl has no safe-search filter"
        return None

    async def _request(self, client: httpx.AsyncClient, query: str) -> object:
        """The one wire call; the single test seam for this source."""
        response = await client.get(MWMBL_SEARCH_URL, params={"q": query})
        response.raise_for_status()
        return response.json()

    async def _fetch(self, query: str, budget: float, headers: dict[str, str]) -> object:
        async with httpx.AsyncClient(
            timeout=budget, headers=headers, follow_redirects=False, trust_env=False
        ) as client:
            return await self._request(client, query)

    async def issue(self, call: SearchCall, ctx: IssueContext) -> IssueOutcome:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        t0 = time.perf_counter()
        try:
            # The one request site: counted immediately before the call so the
            # ledger measures calls issued, not tasks scheduled.
            ctx.ledger.record()
            # wait_for is the wall-clock bound, not the per-operation HTTPX
            # timeouts: a server that trickles a chunk inside the read timeout
            # would otherwise hold this branch, its lease and its slot past the
            # run deadline. The ddgs adapter gets the same bound from the
            # wait_for around its worker thread.
            body = await asyncio.wait_for(
                self._fetch(call.query, min(self._timeout, ctx.budget), headers),
                timeout=ctx.budget,
            )
            results = []
            rank = 1
            # A 200 whose body is not Mwmbl's shape yields no rows rather than an
            # exception: `results` may be absent, null or a non-list, and a row's
            # fields may be any JSON type. Letting a shape variant raise here
            # would turn a quiet upstream into a traceback on stdout's path, so
            # every field that reaches the models is checked.
            rows = body.get("results") if isinstance(body, dict) else []
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                url = row.get("url") if isinstance(row, dict) else None
                if not isinstance(url, str) or not url:
                    continue
                title = row.get("title")
                snippet = row.get("content") or row.get("extract")
                results.append(
                    SearchResult(
                        title=title if isinstance(title, str) else "",
                        url=url,
                        snippet=snippet if isinstance(snippet, str) else "",
                        sources=["mwmbl"],
                        engine_ranks={"mwmbl": rank},
                    )
                )
                rank += 1
                if rank > call.limit:
                    break
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return IssueOutcome(results, latency_ms, None)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return IssueOutcome([], latency_ms, exc)


class GithubSearchAdapter:
    """GitHub repository search over the REST API."""

    provider: str = "github-api"

    def __init__(self, *, token: str | None, timeout: float, user_agent: str) -> None:
        self._token = token
        self._timeout = timeout
        self._user_agent = user_agent

    def precheck(self, call: SearchCall) -> str | None:
        """GitHub is keyless and has no registry gate: always attemptable."""
        return None

    async def issue(self, call: SearchCall, ctx: IssueContext) -> IssueOutcome:
        url = "https://api.github.com/search/repositories"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self._user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        t0 = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=self._timeout, headers=headers, trust_env=False
        ) as client:
            try:
                # The one request site: counted immediately before the call so the
                # ledger measures calls issued, not tasks scheduled.
                ctx.ledger.record()
                response = await client.get(
                    url, params={"q": call.query, "per_page": min(call.limit, 100)}
                )
                response.raise_for_status()
                body = response.json()
                results = []
                rank = 1
                for item in body.get("items", []):
                    if isinstance(item, dict) and item.get("full_name") and item.get("html_url"):
                        results.append(
                            SearchResult(
                                title=item["full_name"],
                                url=item["html_url"],
                                snippet=item.get("description") or "",
                                sources=["github-api"],
                                engine_ranks={"github-api": rank},
                            )
                        )
                        rank += 1
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                return IssueOutcome(results, latency_ms, None)
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                return IssueOutcome([], latency_ms, exc)
