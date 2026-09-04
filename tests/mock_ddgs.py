"""Shared hermetic DDGS double for resilience and adversarial tests."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


class MockDDGSEngine:
    """Configurable mock for ``ddgs.DDGS`` with per-backend responses.

    ``responses`` values may be a result list, an exception instance or class,
    or a callable ``(query, **kwargs) -> list``.
    """

    def __init__(self, responses: dict[str, Any] | None = None):
        self.responses: dict[str, Any] = responses or {}
        self.call_log: list[dict[str, Any]] = []

    def __call__(self, **kwargs):
        return self

    def _dispatch(self, method: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        backend = kwargs.get("backend", "duckduckgo")
        self.call_log.append(
            {"method": method, "query": query, "backend": backend, "kwargs": kwargs}
        )
        if backend in self.responses:
            resp = self.responses[backend]
            if isinstance(resp, BaseException):
                raise resp
            if isinstance(resp, type) and issubclass(resp, BaseException):
                raise resp(f"Simulated failure for {backend}")
            if callable(resp):
                return resp(query, **kwargs)
            return resp
        if method == "news":
            return [
                {
                    "title": f"Breaking News from {backend}",
                    "url": f"https://news.example.com/{backend}-headline",
                    "body": f"News body from {backend}",
                    "date": "2026-09-03T10:00:00Z",
                    "source": backend,
                }
            ]
        return [
            {
                "title": f"Result 1 from {backend}",
                "href": f"https://example.org/topic-{backend}-1",
                "body": f"Snippet 1 for {backend}",
                "date": "2026-09-01T12:00:00Z",
                "source": backend,
            },
            {
                "title": f"Common Topic from {backend}",
                "href": "https://example.org/common-consensus-item",
                "body": f"Consensus snippet from {backend}",
                "date": "2026-09-01T13:00:00Z",
                "source": backend,
            },
        ]

    def text(self, query: str, **kwargs):
        return self._dispatch("text", query, **kwargs)

    def news(self, query: str, **kwargs):
        return self._dispatch("news", query, **kwargs)

    def images(self, query: str, **kwargs):
        return self._dispatch("images", query, **kwargs)


def site_hostname_matches(url: str, site: str) -> bool:
    """Return True when ``url``'s host is ``site`` or a subdomain of it."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host == site or host.endswith(f".{site}")


def freeze_monotonic(monkeypatch, start: float = 1000.0) -> list[float]:
    """Patch ``josty.engine.time.monotonic`` to a mutable clock list."""
    clock = [start]
    monkeypatch.setattr("josty.engine.time.monotonic", lambda: clock[0])
    return clock
