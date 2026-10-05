"""Search-backend health: tell a blocked engine apart from an empty one.

ddgs parses a captcha, a consent wall or a JavaScript shell, finds no results
and returns an empty list *without raising*. The search path therefore sees a
plain empty result and can only report error_kind="empty" -- a blocked engine
is indistinguishable from a quiet one, and no circuit breaker ever trips.

This module makes one direct request per backend to the engine own search URL
and classifies the page instead of the parse. Probing is opt-in (josty --health)
and never runs inside a search, so it adds no hidden amplification to a query.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .status import USER_AGENT

SEARCH_URLS: dict[str, str] = {
    "bing": "https://www.bing.com/search?q={q}",
    "brave": "https://search.brave.com/search?q={q}",
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
    "google": "https://www.google.com/search?q={q}",
    "grokipedia": "https://grokipedia.com/search?q={q}",
    "mojeek": "https://www.mojeek.com/search?q={q}",
    "startpage": "https://www.startpage.com/sp/search?query={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "yahoo": "https://search.yahoo.com/search?p={q}",
}

# A challenge page usually names itself in the title; a real results page can
# mention "captcha" anywhere (Brave's does), so a bare keyword in the body is not
# evidence. Title first, then challenge-specific markup that only a wall ships.
_CHALLENGE_TITLE_TOKENS = (
    "captcha",
    "just a moment",
    "attention required",
    "access denied",
    "forbidden",
    "blocked",
    "are you a robot",
)
_CHALLENGE_TOKENS = (
    "g-recaptcha",
    "hcaptcha",
    "cf-challenge",
    "challenge-platform",
    "solve the captcha",
    "verify you are human",
    "are you a robot",
    "unusual traffic",
)
_CONSENT_TOKENS = ("consent.google", "before you continue", "cookie consent")
_JS_TOKENS = ("enable javascript", "javascript is disabled", "noscript")

_RESULT_MARKERS: dict[str, tuple[str, ...]] = {
    "default": ("<h3", "class=\"result"),
    "bing": ("b_algo", "<h2"),
    "brave": ("result-header", "snippet"),
    "duckduckgo": ("result__a", "result__url"),
    "google": ("/url?q=", "data-snf", "<h3"),
    "grokipedia": ("<article", "search-result"),
    "mojeek": ("results-standard", "class=\"ob\"", "result-title"),
    "startpage": ("w-gl__result", "result-title", "search-result"),
    "wikipedia": ("mw-search-result", "searchresult", "mw-search-results"),
    "yahoo": ("algo-sr", "compTitle", "<h3"),
}

#: A healthy backend parsed real markup out of its own search page.
HEALTHY_STATES = frozenset({"ok"})

#: Probe state to the ErrorKind the search path can report. A challenge, a
#: consent wall and a JavaScript shell are all "the page came back, the engine
#: could not be read"; they map onto the existing error-kind vocabulary rather
#: than inventing a new one.
STATE_TO_ERROR_KIND: dict[str, str] = {
    "blocked": "blocked",
    "challenged": "blocked",
    "consent": "blocked",
    "rate_limited": "rate_limited",
    "js_required": "parse",
    "network": "network",
}

DEFAULT_SNAPSHOT_MAX_AGE_S = 24 * 60 * 60
_SNAPSHOT_MEMORY: dict[str, Any] = {"at": 0.0, "path": None, "states": {}}

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OUTBOUND_LINK_RE = re.compile(r'<a[^>]+href="https?://', re.IGNORECASE)

#: A page this link-dense, with no challenge and no JavaScript wall, answered us.
_RESULT_SHAPED_MIN_LINKS = 8

#: Repeated result markup is what separates a results page from a challenge page.
_RESULT_MARKER_MIN_HITS = 3


@dataclass
class BackendHealth:
    """One backend verdict, with the evidence that produced it."""

    backend: str
    state: str
    http_status: int | None = None
    bytes: int = 0
    title: str = ""
    evidence: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None

    def dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "state": self.state,
            "http_status": self.http_status,
            "bytes": self.bytes,
            "title": self.title,
            "evidence": self.evidence,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }


def classify_page(backend: str, status: int | None, body: str) -> tuple[str, list[str]]:
    """Classify a search page; return the state and the tokens that decided it.

    Status is checked first: 401/403 is a block, 429 is throttling. Body tokens
    are matched case-insensitively, and a challenge outranks result markup
    because a challenge page often ships the engine normal chrome.
    """
    text = body or ""
    lowered = text.lower()
    title = _title_of(text).lower()

    if status in (401, 403):
        return "blocked", ["http " + str(status)]
    if status == 429:
        return "rate_limited", ["http 429"]
    if status is not None and (status < 200 or status >= 400):
        return "network", ["http " + str(status)]

    for token in _CHALLENGE_TITLE_TOKENS:
        if token in title:
            return "challenged", ["title: " + token]

    # Repeated result markup means the engine answered with a results page.
    # One occurrence proves nothing -- challenge pages ship the engine's chrome.
    markers = _RESULT_MARKERS.get(backend, _RESULT_MARKERS["default"])
    hits = {marker: lowered.count(marker) for marker in markers}
    best_marker, best_hits = max(hits.items(), key=lambda item: item[1])
    if best_hits >= _RESULT_MARKER_MIN_HITS:
        return "ok", ["marker x" + str(best_hits) + ": " + best_marker]

    # An unfamiliar but result-shaped page. ddgs parsers go stale when an engine
    # redesigns; that is a parsing failure, not a wall, and the two must not be
    # reported as the same thing.
    links = len(_OUTBOUND_LINK_RE.findall(text))
    if links >= _RESULT_SHAPED_MIN_LINKS and not any(
        token in lowered for token in _CHALLENGE_TOKENS
    ):
        return "ok", ["generic: " + str(links) + " outbound links"]

    for token in _CHALLENGE_TOKENS:
        if token in lowered:
            return "challenged", [token]
    for token in _CONSENT_TOKENS:
        if token in lowered:
            return "consent", [token]
    for token in _JS_TOKENS:
        if token in lowered:
            return "js_required", [token]

    return "empty", []


def _title_of(body: str) -> str:
    match = _TITLE_RE.search(body or "")
    return match.group(1).strip()[:80] if match else ""


def probe_backend(
    backend: str,
    query: str,
    *,
    fetch: Callable[[str], tuple[int | None, str, float]],
) -> BackendHealth:
    """Probe one backend search page through an injected fetcher.

    fetch returns (http_status, body, latency_ms) so tests can drive every
    classification without touching the network.
    """
    url_template = SEARCH_URLS.get(backend)
    if url_template is None:
        return BackendHealth(backend, "unmapped", evidence=["no search URL known"])
    url = url_template.format(q=query.replace(" ", "+"))
    try:
        status, body, latency = fetch(url)
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not raise
        return BackendHealth(
            backend, "network", error=type(exc).__name__ + ": " + str(exc)[:160]
        )
    state, evidence = classify_page(backend, status, body)
    return BackendHealth(
        backend,
        state,
        http_status=status,
        bytes=len(body or ""),
        title=_title_of(body or ""),
        evidence=evidence,
        latency_ms=latency,
    )


def _http_fetch(timeout: float) -> Callable[[str], tuple[int | None, str, float]]:
    """Fetch the way the search path does.

    Plain httpx has a Python TLS/HTTP2 fingerprint, and the engines that deploy
    bot management answer it with a challenge even when the same request from an
    impersonating client succeeds. ddgs fetches through primp with Chrome
    impersonation, so the probe must too -- otherwise it reports the probe's own
    fingerprint, not the backend's health.
    """
    try:
        import primp  # noqa: PLC0415 - optional, ships with ddgs
    except ImportError:
        primp = None  # type: ignore[assignment]

    if primp is not None:
        client = primp.Client(impersonate="chrome")

        def fetch_impersonated(url: str) -> tuple[int | None, str, float]:
            started = time.perf_counter()
            response = client.get(url, timeout=int(timeout))
            return (
                response.status_code,
                response.text,
                (time.perf_counter() - started) * 1000,
            )

        return fetch_impersonated

    def fetch(url: str) -> tuple[int | None, str, float]:
        started = time.perf_counter()
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        return (
            response.status_code,
            response.text,
            (time.perf_counter() - started) * 1000,
        )

    return fetch


def snapshot_path() -> Path:
    """Where a health snapshot is kept. Override with JOSTY_HEALTH_SNAPSHOT."""
    override = os.getenv("JOSTY_HEALTH_SNAPSHOT")
    if override:
        return Path(override)
    base = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "josty"
    return base / "health.json"


def save_snapshot(report: dict[str, Any], path: Path | None = None) -> Path:
    """Persist a report so the search path can report what the probe proved."""
    target = path or snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.time(),
        "states": {b["backend"]: b["state"] for b in report.get("backends", [])},
    }
    target.write_text(json.dumps(payload, indent=1))
    return target


def load_snapshot(
    *,
    max_age_s: float = DEFAULT_SNAPSHOT_MAX_AGE_S,
    path: Path | None = None,
) -> dict[str, str]:
    """Return the last snapshot's backend states, or {} when stale or absent.

    The parsed snapshot is cached for a minute in-process: a single search asks
    this question once per backend branch, and re-reading a file per branch
    would be waste.
    """
    now = time.time()
    target = path or snapshot_path()
    if (
        _SNAPSHOT_MEMORY["path"] == str(target)
        and now - _SNAPSHOT_MEMORY["at"] < 60
    ):
        return dict(_SNAPSHOT_MEMORY["states"])
    states: dict[str, str] = {}
    try:
        payload = json.loads(target.read_text())
        if now - float(payload.get("generated_at", 0)) <= max_age_s:
            states = {
                str(k): str(v) for k, v in (payload.get("states") or {}).items()
            }
    except (OSError, ValueError, TypeError):
        states = {}
    _SNAPSHOT_MEMORY.update({"at": now, "path": str(target), "states": states})
    return dict(states)


def known_error_kind(backend: str, *, max_age_s: float = DEFAULT_SNAPSHOT_MAX_AGE_S) -> str | None:
    """ErrorKind for a backend whose last probe found it unreadable, else None."""
    state = load_snapshot(max_age_s=max_age_s).get(backend)
    return STATE_TO_ERROR_KIND.get(state or "")


def run_health(
    backends: list[str] | tuple[str, ...],
    *,
    query: str = "josty search health",
    timeout: float = 20.0,
    fetch: Callable[[str], tuple[int | None, str, float]] | None = None,
    save: bool = False,
) -> dict[str, Any]:
    """Probe every backend once and return a JSON-safe report."""
    fetcher = fetch or _http_fetch(timeout)
    probes = [probe_backend(name, query, fetch=fetcher) for name in backends]
    healthy = [p.backend for p in probes if p.state in HEALTHY_STATES]
    blocked = [
        p.backend for p in probes if p.state in {"blocked", "challenged", "consent"}
    ]
    report = {
        "schema_version": "1.0",
        "phase": "search",
        "probe": "engine_search_page",
        "status": "complete",
        "query": query,
        "count": len(probes),
        "healthy": healthy,
        "blocked": blocked,
        "backends": [p.dict() for p in probes],
        "note": (
            "One direct request per backend to its own search URL. A challenged, "
            "consent-walled or JavaScript-only page is reported as such; ddgs would "
            "return an empty list for all three and call it a quiet backend."
        ),
    }
    if save:
        # A read-only cache directory must not fail a diagnostic. Record what
        # happened instead, so the caller can say the snapshot was not kept.
        try:
            report["snapshot"] = str(save_snapshot(report))
        except OSError as exc:
            report["snapshot"] = None
            report["snapshot_error"] = type(exc).__name__ + ": " + str(exc)[:120]
    return report
