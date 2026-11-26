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
import html
import re
import socket
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote_plus, urlsplit

import httpx

from .branch import IssueContext, IssueOutcome, SearchCall
from .errors import ProviderError, _is_challenge_page
from .fetch import is_ad_redirect
from .models import SearchResult, WireFacts, WireResponse
from .status import SearchCategory

if TYPE_CHECKING:
    from .fanout import _FanoutLedger
    from .lease import LeasePool


#: Yahoo's second results markup (#100). Captured 2026-11-26 through ddgs's own
#: Yahoo engine: one query flips between two pages. ddgs 9.16.1 reads only the
#: first (div.relsrch items with a div.Title); the other carries section.s-algo
#: items with a div.compTitle and a div.compText.
_YAHOO_VARIANT_ITEMS = "//section[contains(@class, 's-algo')]"
#: The h3 also holds a span.title-url with the site name and the display URL.
_YAHOO_VARIANT_TITLE = (
    ".//div[contains(@class, 'compTitle')]//h3[1]"
    "//text()[not(ancestor::span[contains(@class, 'title-url')])]"
)
_YAHOO_VARIANT_LINKS = ".//div[contains(@class, 'compTitle')]//a/@href"
_YAHOO_VARIANT_TEXT = ".//div[contains(@class, 'compText')]"


def _yahoo_variant_rows(body: str) -> list[dict[str, Any]]:
    """Rows from Yahoo's second markup; empty when the page has none.

    Fitted to the capture, never invented. Ads and the AI and knowledge modules
    carry no /RU= redirect and are dropped.
    """
    try:
        import lxml.html
    except ImportError:  # pragma: no cover - lxml is a ddgs dependency
        return []
    try:
        document = lxml.html.fromstring(body)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for item in document.xpath(_YAHOO_VARIANT_ITEMS):
        parts = item.xpath(_YAHOO_VARIANT_TITLE)
        title = " ".join(part.strip() for part in parts if part.strip())
        link = next((href for href in item.xpath(_YAHOO_VARIANT_LINKS) if "/RU=" in href), None)
        if not link or not title:
            continue
        url = unquote_plus(link.split("/RU=", 1)[1].split("/RK=", 1)[0].split("/RS=", 1)[0])
        if not url.startswith(("http://", "https://")) or "bing.com/aclick" in url:
            continue
        snippets = item.xpath(_YAHOO_VARIANT_TEXT)
        text = " ".join(snippets[0].itertext()) if snippets else ""
        rows.append({"title": title, "href": url, "body": re.sub(r"\s+", " ", text).strip()})
    return rows


def _json_body(response: httpx.Response, source: str, query: str) -> object:
    """The body as JSON. HTML in its place is a challenge page or a parse miss."""
    try:
        return response.json()
    except ValueError as exc:
        kind = "blocked" if _is_challenge_page(response.text, query) else "parse"
        raise ProviderError(
            kind, f"{source}: HTTP {response.status_code} body is not JSON"
        ) from exc


class _WireTap:
    """Records what the engines behind one ddgs call actually got back (#99).

    ddgs answers every non-200 with ``None`` and a flat "No results found.". The
    tap wraps the HTTP client of each engine the call will use, so the status,
    the body size and whether the body is an interstitial survive to
    classification. It is installed on the call's own engine instances (Josty
    builds a fresh DDGS client per call), so nothing here is shared between
    threads. Every seam it touches is private to ddgs and pinned by a compat
    test; a client without the seam (the suite's doubles) is simply not tapped.
    """

    def __init__(self, query: str | None = None) -> None:
        self.facts = WireFacts()
        #: The call's query, so a page title can be read without it (#99).
        self.query = query

    def install(self, ddgs: Any, call: SearchCall) -> None:
        """Tap every engine the call will use. Never raises: no seam, no tap."""
        try:
            get_engines = getattr(ddgs, "_get_engines", None)
            if get_engines is None:
                return
            for engine in get_engines(call.category, call.backend):
                http_client = getattr(engine, "http_client", None)
                request = getattr(http_client, "request", None)
                if request is not None:
                    http_client.request = self._recording(request)
        except Exception:
            return

    def _recording(self, request: Callable[..., Any]) -> Callable[..., Any]:
        """A drop-in for ``HttpClient.request`` that notes what comes back."""

        def tapped(*args: Any, **kwargs: Any) -> Any:
            url = args[1] if len(args) > 1 else kwargs.get("url")
            host = urlsplit(str(url)).hostname if url else None
            if host and self.facts.host is None:
                self.facts.host = host
            response = request(*args, **kwargs)
            status = getattr(response, "status_code", None)
            if isinstance(status, int):
                body = getattr(response, "text", None)
                size = getattr(response, "content", None)
                text = body if isinstance(body, str) else ""
                self.facts.responses.append(
                    WireResponse(
                        status,
                        len(size) if isinstance(size, (bytes, bytearray)) else len(text),
                        status == 200 and _is_challenge_page(text, self.query),
                    )
                )
                if status == 200:
                    # Kept only so an adapter can re-parse a page it has (#100).
                    self.facts.body = text
            return response

        return tapped

    def note_silence(self) -> WireFacts:
        """Explain a call that got no HTTP response: where did its host resolve?

        The lookup runs on a daemon thread and is waited for at most
        ``_SILENCE_LOOKUP_SECONDS``: a slow resolver must not hold this worker
        and its lease past the search timeout. A lookup that does not finish
        in time leaves ``resolved`` empty.
        """
        if self.facts.host and not self.facts.responses:
            found: list[tuple[str, ...]] = []
            lookup = threading.Thread(
                target=lambda host=self.facts.host: found.append(_resolve(host)), daemon=True
            )
            lookup.start()
            lookup.join(_SILENCE_LOOKUP_SECONDS)
            self.facts.resolved = found[0] if found else ()
        return self.facts


#: Ceiling on the after-the-fact DNS lookup of a call that got no response.
_SILENCE_LOOKUP_SECONDS = 1.0


def _resolve(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return ()
    return tuple(dict.fromkeys(info[4][0] for info in infos))


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
            results, latency_ms, exc, wire = await asyncio.wait_for(
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
        return IssueOutcome(results, latency_ms, exc, wire=wire)

    def _worker(
        self,
        call: SearchCall,
        *,
        ledger: _FanoutLedger,
        pool: LeasePool | None,
        lease_id: int | None,
    ) -> tuple[list[SearchResult], float, Exception | None, WireFacts | None]:
        """One blocking engine call, run on a worker thread.

        The fourth element is what the wire said (#99), or None when the client
        offers no seam to tap.

        Always retires its own lease, including on the exception path. The release
        is idempotent, so returning *after* the arbiter already reaped this lease
        as a ghost is a safe no-op rather than a double free.
        """
        t0 = time.perf_counter()
        tap = _WireTap(call.query)
        try:
            # A fresh DDGS client per call is deliberate, not waste:
            # ddgs engine instances carry a shared cached_property lxml
            # parser, which is not thread-safe. Caching one client per
            # backend would let concurrent query variants of the same
            # engine parse HTML on one parser — the same C-level
            # corruption class already fixed for trafilatura extraction.
            ddgs = self._client_factory(timeout=self._timeout)
            # See what the engines get back; ddgs itself will not say (#99).
            tap.install(ddgs, call)
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
            try:
                rows = method(call.query, **kwargs)
            except Exception:
                # ddgs parses one of Yahoo's two markups; the other is read from
                # the bytes the tap already holds (#100). Not a retry.
                if call.backend != "yahoo" or not tap.facts.body:
                    raise
                rows = _yahoo_variant_rows(tap.facts.body)
                if not rows:
                    raise
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
            latency = round((time.perf_counter() - t0) * 1000, 2)
            return results, latency, None, tap.facts
        except Exception as exc:
            latency = round((time.perf_counter() - t0) * 1000, 2)
            return [], latency, exc, tap.note_silence()
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
        return _json_body(response, "mwmbl", query)

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


#: Wikipedia's ``list=search`` API: keyless JSON, a full-text search. ddgs reaches
#: the wiki through ``action=opensearch`` instead, which matches title prefixes
#: with ``limit=1``.
WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
_WIKI_TITLE_URL = "https://en.wikipedia.org/wiki/"
_SEARCHMATCH_TAG_RE = re.compile(r"<[^>]*>")


class WikipediaSearchAdapter:
    """English Wikipedia's keyless ``list=search`` API.

    ddgs asks ``action=opensearch``, which matches title prefixes only: "python
    asyncio timeout" is a miss of that index and reads as "not found".
    ``list=search`` is a full-text search, and ``searchinfo.totalhits`` settles
    what an empty page means: 0 is a real miss, a positive count with no rows is
    a parse problem. The API takes no date, region or safe-search parameter, so
    :meth:`precheck` refuses a call whose filter it cannot honour.
    """

    provider: str = "wikipedia"

    def __init__(self, *, timeout: float, user_agent: str) -> None:
        self._timeout = timeout
        self._user_agent = user_agent

    def precheck(self, call: SearchCall) -> str | None:
        """Refuse a call whose filter this API cannot honour, or None to proceed."""
        if call.category != "text":
            return "skipped: wikipedia serves web search only, not news"
        if call.timelimit is not None:
            return "skipped: wikipedia does not support time-limited search"
        if call.region is not None:
            return "skipped: wikipedia is English-only and ignores region selection"
        if call.safesearch == "on":
            return "skipped: wikipedia has no safe-search filter"
        return None

    async def _request(self, client: httpx.AsyncClient, query: str, limit: int) -> object:
        """The one wire call; the single test seam for this source."""
        response = await client.get(
            WIKIPEDIA_SEARCH_URL,
            params={
                "action": "query",
                "list": "search",
                "format": "json",
                # The API's ceiling for a client without a bot flag.
                "srlimit": min(limit, 500),
                "srsearch": query,
            },
        )
        response.raise_for_status()
        return _json_body(response, "wikipedia", query)

    async def _fetch(
        self, query: str, limit: int, budget: float, headers: dict[str, str]
    ) -> object:
        async with httpx.AsyncClient(
            timeout=budget, headers=headers, follow_redirects=False, trust_env=False
        ) as client:
            return await self._request(client, query, limit)

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
            body = await asyncio.wait_for(
                self._fetch(call.query, call.limit, min(self._timeout, ctx.budget), headers),
                timeout=ctx.budget,
            )
            # Every field that reaches the models is checked: a shape variant
            # must not raise on the path to stdout.
            page = body.get("query") if isinstance(body, dict) else None
            page = page if isinstance(page, dict) else {}
            info = page.get("searchinfo")
            total = info.get("totalhits") if isinstance(info, dict) else None
            rows = page.get("search")
            results = []
            rank = 1
            for row in rows if isinstance(rows, list) else []:
                title = row.get("title") if isinstance(row, dict) else None
                if not isinstance(title, str) or not title:
                    continue
                snippet = row.get("snippet")
                if isinstance(snippet, str):
                    # The API highlights the match with span tags and escapes
                    # entities; a snippet is plain text.
                    snippet = html.unescape(_SEARCHMATCH_TAG_RE.sub("", snippet))
                else:
                    snippet = ""
                results.append(
                    SearchResult(
                        title=title,
                        # What the API's own links leave unescaped, besides "/".
                        url=_WIKI_TITLE_URL
                        + quote(title.replace(" ", "_"), safe="/!" + "'()*,:;=@&"),
                        snippet=snippet,
                        sources=["wikipedia"],
                        engine_ranks={"wikipedia": rank},
                    )
                )
                rank += 1
                if rank > call.limit:
                    break
            if not results and total != 0:
                # searchinfo.totalhits settles what the empty page means: 0 is a
                # certified miss; anything else, including "absent", is not.
                raise ProviderError(
                    "parse",
                    f"wikipedia: searchinfo.totalhits={total!r} but no rows returned",
                )
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
