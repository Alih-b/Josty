"""Search result, provider, host, and run envelope models."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .fetch import text_quote
from .status import (
    DIAGNOSE_NOTE,
    PROBLEM_TYPE_BASE,
    SCHEMA_VERSION,
    ErrorKind,
    SearchStatus,
    index_of,
)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    sources: list[str] = field(default_factory=list)
    published_at: str | None = None
    publisher: str | None = None
    score: float = 0.0
    content: str | None = None
    extraction_method: str | None = None
    fetched_url: str | None = None
    fetched_at: str | None = None
    fetch_error: str | None = None
    engine_ranks: dict[str, int] = field(default_factory=dict)
    rank_contributions: dict[str, float] = field(default_factory=dict)
    score_weights: dict[str, float] = field(default_factory=dict)

    #: The per-item fetch envelope, in output order. Owned here because these are
    #: this type's fields; the CLI only prints the mapping.
    FETCH_FIELDS = (
        "url",
        "content",
        "extraction_method",
        "fetched_url",
        "fetched_at",
        "fetch_error",
    )

    def dict(self) -> dict[str, Any]:
        return asdict(self)

    def quote(self, phrase: str) -> Quote | None:
        """A citable span for an exact phrase of the stored page text (#107).

        None when the page was not fetched or the text does not contain the
        phrase: never a fuzzy near-match.
        """
        text = self.content
        if not text:
            return None
        selector = text_quote(text, phrase)
        if selector is None:
            return None
        return Quote(
            url=self.fetched_url or self.url,
            fetched_at=self.fetched_at,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            selector=selector,
        )

    def fetch_dict(self) -> dict[str, Any]:
        """The per-item fetch envelope, one definition for every stdin shape."""
        return {name: getattr(self, name) for name in self.FETCH_FIELDS}

    @classmethod
    def from_rows(cls, rows: list, limit: int | None = None) -> list[SearchResult]:
        """Build fetch items from JSON result objects.

        Shared by both JSON stdin shapes (a ``{"results": [...]}`` envelope and a
        bare array), so an entry is interpreted identically either way: only dict
        entries with a non-empty ``url`` become items. ``limit`` is applied here so
        both shapes honour ``--limit`` in one place.
        """
        if limit is not None and limit > 0:
            rows = rows[:limit]
        return [
            cls(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet", ""),
                sources=r.get("sources", []),
                published_at=r.get("published_at"),
                publisher=r.get("publisher"),
                score=r.get("score", 0.0),
                content=r.get("content"),
                extraction_method=r.get("extraction_method"),
                fetched_url=r.get("fetched_url"),
                fetched_at=r.get("fetched_at"),
                fetch_error=r.get("fetch_error"),
                engine_ranks=r.get("engine_ranks", {}),
                rank_contributions=r.get("rank_contributions", {}),
                score_weights=r.get("score_weights", {}),
            )
            for r in rows
            if isinstance(r, dict) and r.get("url")
        ]


@dataclass(frozen=True)
class Quote:
    """A citable span of a fetched page (#107).

    A snippet or bounded page text lets an agent claim a page said something,
    and nothing later can check that claim. A quote pins the exact phrase with
    32 characters of context on each side (a W3C TextQuoteSelector) and the
    hash of the stored text, so a later re-fetch can show whether the page
    drifted. It is honest about the extracted text, not about the DOM.
    """

    url: str
    fetched_at: str | None
    sha256: str
    selector: dict[str, str]

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderStatus:
    provider: str
    query: str
    ok: bool
    result_count: int = 0
    error: str | None = None
    error_kind: ErrorKind | None = None
    # Worst-case across concurrent query variants, not a representative sample.
    latency_ms: float | None = None
    circuit_state: str | None = None
    failures: int | None = None
    backoff_remaining: float | None = None
    # What the wire said (#99), when Josty could see it. None when no HTTP
    # response was observed: a timeout, a refused call, or a source without a tap.
    http_status: int | None = None
    body_bytes: int | None = None
    #: Share of the engine's wire-reaching calls (all its variants) that got a
    #: non-200. ddgs issues one HTTP request per call, so calls and sub-requests
    #: coincide. None when no call reached the wire.
    refusal_rate: float | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WireResponse:
    """One HTTP response an adapter observed behind a provider call."""

    status_code: int
    body_bytes: int
    #: True when a 200 body is an interstitial (captcha, Anubis, Cloudflare).
    challenged: bool = False


@dataclass
class WireFacts:
    """What the HTTP exchange behind one provider call actually said (#99).

    ddgs answers every non-200 with ``None`` and a flat "No results found.", so
    a 429, a block page and a genuine miss were indistinguishable. An adapter
    that can see the wire fills this in; :mod:`josty.errors` reads it.
    """

    #: Every HTTP response of the call, in wire order.
    responses: list[WireResponse] = field(default_factory=list)
    #: Host the call asked, once its first request went out.
    host: str | None = None
    #: Addresses the host resolved to when nothing came back; empty otherwise.
    resolved: tuple[str, ...] = ()
    #: Text of the last 200 body, kept only so an adapter can re-parse a page it
    #: already fetched (#100). Never serialized.
    body: str | None = None

    @property
    def http_status(self) -> int | None:
        """The first non-200 of the call, else the first response's status."""
        refused = [
            response.status_code
            for response in self.responses
            if response.status_code != 200
        ]
        if refused:
            return refused[0]
        return self.responses[0].status_code if self.responses else None

    @property
    def body_bytes(self) -> int | None:
        """Size of the largest body of the call."""
        return max((response.body_bytes for response in self.responses), default=None)

    @property
    def refusal_rate(self) -> float | None:
        """Share of the call's HTTP sub-requests that were not a 200."""
        if not self.responses:
            return None
        refused = sum(1 for response in self.responses if response.status_code != 200)
        return round(refused / len(self.responses), 3)


@dataclass
class HostStatus:
    provider: str
    host: str
    ok: bool
    http_status: int | None = None
    error_kind: Literal["timeout", "dns", "tls", "network", "unknown", "skipped"] | None = None
    error: str | None = None
    challenged: bool = False
    latency_ms: float | None = None
    circuit_state: str | None = None
    failures: int | None = None
    backoff_remaining: float | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnoseRun:
    providers: list[HostStatus] = field(default_factory=list)

    @property
    def reachable(self) -> int:
        return sum(provider.ok for provider in self.providers)

    @property
    def status(self) -> str:
        if not self.providers or self.reachable == 0:
            return "failed"
        if self.reachable < len(self.providers):
            return "degraded"
        return "complete"

    def dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": "transport",
            "probe": "https_host",
            "status": self.status,
            "reachable": self.reachable,
            "count": len(self.providers),
            "note": DIAGNOSE_NOTE,
            "providers": [provider.dict() for provider in self.providers],
        }


@dataclass(frozen=True)
class Absence:
    """What a run without rows knows (#106).

    ``novalue``: an engine answered and had nothing, so the index was asked.
    ``somevalue``: no engine answered, so the page may exist unseen.
    """

    kind: Literal["novalue", "somevalue"]
    asked: list[str]
    answered: list[str]


@dataclass
class SearchRun:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    providers: list[ProviderStatus] = field(default_factory=list)
    cached: bool = False
    run_at: str | None = None  # ISO8601 UTC moment the search was executed
    query_variant_count: int | None = None
    request_count: int | None = None
    # Fanout admission accounting. `request_count` is the measured number of calls
    # that reached the ddgs/GitHub call site; `scheduled_count` is how many were
    # proposed; the difference is calls that were never issued, split by why.
    # `shed_*` counts refused admission (capacity / ghost_capacity / ghost_budget /
    # deadline). A shed call never opened a socket and is reported as `skipped`,
    # never as a network failure. `ghosts_*` count workers that outlived their
    # lease and had not returned when the fanout finished.
    scheduled_count: int | None = None
    shed_count: int = 0
    shed_by_reason: dict[str, int] = field(default_factory=dict)
    ghosts_outstanding: int = 0
    ghosts_peak: int = 0
    fetch_requested: bool = False
    fetch_attempted: int = 0
    fetch_ok: int = 0
    fetch_failed: int = 0
    #: True when the rows are an expired cached answer, served because no engine
    #: answered this run (#104). The providers are always this run's own.
    stale: bool = False
    age_seconds: float | None = None
    #: Engines the walk never asked (#105). Not failures: they were never issued.
    untried: list[str] = field(default_factory=list)
    #: The run's maker and its parameters, so more() can issue one more step
    #: (#106). Never serialized.
    _maker: Any = field(default=None, repr=False, compare=False)

    @property
    def provider_count(self) -> int:
        return len(self.providers)

    @property
    def nonempty_provider_count(self) -> int:
        return sum(
            1
            for provider in self.providers
            if provider.ok and provider.result_count > 0
        )

    @property
    def coverage(self) -> float | None:
        n = len(self.providers)
        if n == 0:
            return None
        return round(self.nonempty_provider_count / n, 3)

    @property
    def fetch_status(self) -> str:
        if not self.fetch_requested:
            return "skipped"
        # Requested but the SERP produced nothing to fetch: the phase neither ran
        # nor failed, so it is "noop", not "skipped" (which means not requested).
        if self.fetch_attempted == 0:
            return "noop"
        if self.fetch_ok == 0:
            return "failed"
        if self.fetch_failed > 0:
            return "degraded"
        return "complete"

    @property
    def partial(self) -> bool:
        # A provider is a failed branch when its call failed outright, or when it
        # answered but an aggregated query variant failed (ok=true with a failure
        # error_kind). "empty" is a successful empty branch, not a failure.
        search_partial = any(
            not provider.ok or provider.error_kind not in (None, "empty")
            for provider in self.providers
        )
        # Fetch is a separate phase: a total extraction miss must not look like
        # a clean search. Partial fetch success stays on the search status and
        # is visible on fetch.{ok,failed,status}.
        fetch_total_miss = (
            self.fetch_requested and self.fetch_attempted > 0 and self.fetch_ok == 0
        )
        return search_partial or fetch_total_miss

    @property
    def usable(self) -> bool:
        """True when the run carries results a caller can use."""
        return bool(self.results)

    @property
    def status(self) -> str:
        if not self.results and self.providers and all(not item.ok for item in self.providers):
            return SearchStatus.FAILED
        if self.partial:
            return SearchStatus.DEGRADED
        if not self.results:
            return SearchStatus.EMPTY
        return SearchStatus.COMPLETE

    def __repr__(self) -> str:
        """The agent's view (#108): a header, what did not answer, the top rows.

        No JSON. At most two lines before the result lines, and one result line
        per row, so the length follows --limit.
        """
        indexes = sorted({index_of(name) for result in self.results for name in result.sources})
        header = f"{self.query} · {len(self.results)} results"
        if indexes:
            plural = "es" if len(indexes) != 1 else ""
            header += f" · {len(indexes)} index{plural} ({', '.join(indexes)})"
        if self.untried:
            header += f" · {len(self.untried)} untried"
        lines = [header]
        silent = [
            f"{p.provider} {p.error_kind}" + (f" {p.http_status}" if p.http_status else "")
            for p in self.providers
            if not p.ok
        ]
        if silent:
            lines.append("no answer: " + ", ".join(silent))
        for number, result in enumerate(self.results, 1):
            found_by = len({index_of(name) for name in result.sources})
            title = (result.title or result.url).replace("\n", " ")[:60]
            lines.append(f"{number:>2} {'✓' * found_by} {title}   {', '.join(result.sources)}")
        return "\n".join(lines)

    def why(self, engine: str) -> dict[str, Any] | None:
        """Why an engine did not contribute, as an RFC 9457 problem (#106).

        None when it answered, or when the run never heard of it. The type ends
        in a stable fragment per kind (#rate-limited, #blocked, #untried);
        status is the HTTP status the wire saw, when it saw one.
        """
        if engine in self.untried:
            kind, detail, status = "untried", "not asked: the walk stopped first", None
        else:
            provider = next((p for p in self.providers if p.provider == engine), None)
            if provider is None or provider.ok:
                return None
            kind = provider.error_kind or "unknown"
            detail, status = provider.error, provider.http_status
        problem: dict[str, Any] = {
            "type": f"{PROBLEM_TYPE_BASE}#{kind.replace('_', '-')}",
            "title": "Engine did not answer",
            "engine": engine,
        }
        if status is not None:
            problem["status"] = status
        if detail:
            problem["detail"] = detail
        return problem

    def only(self, source: str) -> list[SearchResult]:
        """Rows one source found: an engine's name, or the index it serves."""
        return [
            result
            for result in self.results
            if any(name == source or index_of(name) == source for name in result.sources)
        ]

    def agree(self, indexes: int = 2) -> list[SearchResult]:
        """Rows that at least n distinct search indexes found. Usually few."""
        return [
            result
            for result in self.results
            if len({index_of(name) for name in result.sources}) >= indexes
        ]

    @property
    def absent(self) -> Absence | None:
        """Why there are no rows (#106); None when there are."""
        if self.results:
            return None
        answered = [provider.provider for provider in self.providers if provider.ok]
        asked = [provider.provider for provider in self.providers]
        return Absence("novalue" if answered else "somevalue", asked, answered)

    async def more(self) -> SearchRun | None:
        """Ask the next engine the walk left out (#106).

        This run is left as it is; the answer is a run of its own, carrying the
        engines still untried. None when nothing is untried, or for a run that
        came back from the cache.
        """
        if self._maker is None or not self.untried:
            return None
        return await self._maker[0]._more_of(self, self._maker[1])

    def dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "query": self.query,
            "status": self.status,
            "count": len(self.results),
            "partial": self.partial,
            "cached": self.cached,
            "provider_count": self.provider_count,
            "nonempty_provider_count": self.nonempty_provider_count,
            "coverage": self.coverage,
            "query_variant_count": self.query_variant_count,
            "request_count": self.request_count,
            "fanout": {
                "scheduled": self.scheduled_count,
                "issued": self.request_count,
                "shed": self.shed_count,
                "shed_by_reason": dict(self.shed_by_reason),
                "ghosts_outstanding": self.ghosts_outstanding,
                "ghosts_peak": self.ghosts_peak,
            },
            "fetch": {
                "requested": self.fetch_requested,
                "attempted": self.fetch_attempted,
                "ok": self.fetch_ok,
                "failed": self.fetch_failed,
                "status": self.fetch_status,
            },
            "providers": [provider.dict() for provider in self.providers],
            "results": [result.dict() for result in self.results],
            "untried": list(self.untried),
        }
        if self.run_at is not None:
            payload["run_at"] = self.run_at
        if self.stale:
            # Only on a stale run: every other payload stays the 1.0 envelope.
            payload["stale"] = True
            payload["age_seconds"] = self.age_seconds
        return payload
