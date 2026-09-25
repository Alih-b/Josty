"""Search result, provider, host, and run envelope models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .status import DIAGNOSE_NOTE, SCHEMA_VERSION, ErrorKind, SearchStatus


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

    def dict(self) -> dict[str, Any]:
        return asdict(self)


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
        }
        if self.run_at is not None:
            payload["run_at"] = self.run_at
        return payload
