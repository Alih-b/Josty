"""Shared type aliases, run-level status values, and cross-cutting constants."""

from __future__ import annotations

from typing import Literal

from ._version import __version__

SearchMode = Literal["plain", "exact"]
SearchCategory = Literal["text", "news"]
SafeSearch = Literal["on", "moderate", "off"]
TimeLimit = Literal["d", "w", "m", "y"]
ErrorKind = Literal[
    "network", "rate_limited", "blocked", "empty", "parse", "unknown", "skipped", "filtered", "geo"
]
ProfileType = Literal["general", "dev"]

SCHEMA_VERSION = "1.0"

#: Base of the problem types SearchRun.why() returns (#106); the fragment names
#: the kind, so it stays stable whatever words the detail uses.
PROBLEM_TYPE_BASE = "https://github.com/Alih-b/Josty/problems"


class SearchStatus:
    """Run-level search status values; the wire schema stays 1.0."""

    COMPLETE = "complete"
    DEGRADED = "degraded"
    EMPTY = "empty"
    FAILED = "failed"


MAX_SITES = 5
CHALLENGED_HTTP_STATUSES = frozenset({401, 403, 429})
DIAGNOSE_NOTE = (
    "HTTPS homepage reachability only; not search-backend health. "
    "Search can succeed while this reports failed or degraded."
)
# Headroom over the DDGS client timeout so ddgs's own TimeoutException fires
# first where possible; asyncio.wait_for is the outer belt, not the inner one.
SEARCH_THREAD_TIMEOUT_HEADROOM = 2.0
USER_AGENT = f"josty/{__version__} (+https://github.com/Alih-b/Josty)"

# -- the engine walk (#105) ------------------------------------------------------
#: The order a run asks engines in, measured across four egresses: yandex, mwmbl
#: and the GitHub API answered on every route; google, mojeek and startpage on
#: none. An engine that fails leaves the front on its own: the breaker suspends it.
ENGINE_PRIORS: tuple[str, ...] = (
    "yandex",
    "mwmbl",
    "github-api",
    "bing",
    "wikipedia",
    "yahoo",
    "brave",
    "duckduckgo",
    "mojeek",
    "startpage",
    "grokipedia",
)
#: Which search index an engine really is, where that is not its own name.
SHARED_INDEX: dict[str, str] = {
    "yahoo": "bing",
    "duckduckgo": "bing",
    "startpage": "google",
}
#: How many distinct search indexes a run stops after.
WANT_INDEXES = 2


def index_of(engine: str) -> str:
    """The search index an engine serves."""
    return SHARED_INDEX.get(engine, engine)


def engine_walk_order(engines: list[str]) -> list[str]:
    """Engines in the order the walk asks them: by prior, unknown ones last."""
    return sorted(
        engines,
        key=lambda engine: ENGINE_PRIORS.index(engine)
        if engine in ENGINE_PRIORS
        else len(ENGINE_PRIORS),
    )
