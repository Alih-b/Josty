"""Bounded SQLite SERP cache and envelope (de)serialization."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import ProviderStatus, SearchResult, SearchRun

CACHE_MAX_ROWS = 5000
CACHE_PRUNE_BATCH = 500
CACHE_MAX_BYTES = 50_000_000


class SearchCache:
    """Lightweight SQLite-backed cache for search runs with TTL."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        default_ttl: float = 21600.0,
        max_rows: int = CACHE_MAX_ROWS,
        prune_batch: int = CACHE_PRUNE_BATCH,
        max_bytes: int = CACHE_MAX_BYTES,
    ):
        self.disabled = False
        self.db_path: Path | None
        if db_path is None:
            cache_dir = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "josty"
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = cache_dir / "cache.db"
            except Exception:
                # Fail closed: never fall back to a world-shared /tmp path.
                self.disabled = True
                self.db_path = None
        else:
            self.db_path = Path(db_path)
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                self.disabled = True
                self.db_path = None
        self.default_ttl = default_ttl
        self.max_rows = max_rows
        self.prune_batch = prune_batch
        self.max_bytes = max_bytes
        if not self.disabled:
            self._init_db()
            self._restrict_db_mode()

    def _restrict_db_mode(self) -> None:
        if self.db_path is None:
            return
        with suppress(OSError):
            if self.db_path.exists():
                os.chmod(self.db_path, 0o600)

    def _get_conn(self) -> sqlite3.Connection:
        if self.disabled or self.db_path is None:
            raise sqlite3.OperationalError("search cache is disabled")
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with suppress(Exception), self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_cache (
                    key TEXT PRIMARY KEY,
                    created_at REAL,
                    expires_at REAL,
                    payload TEXT,
                    hit_count INTEGER DEFAULT 0,
                    last_accessed REAL DEFAULT 0
                );
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(search_cache)").fetchall()
            }
            if "hit_count" not in columns:
                conn.execute(
                    "ALTER TABLE search_cache ADD COLUMN hit_count INTEGER DEFAULT 0"
                )
            if "last_accessed" not in columns:
                conn.execute(
                    "ALTER TABLE search_cache ADD COLUMN last_accessed REAL DEFAULT 0"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_expires_at ON search_cache(expires_at);"
            )

    @staticmethod
    def hash_key(query: str, **kwargs: Any) -> str:
        serialized = json.dumps(
            {"q": query.strip().lower(), **kwargs}, sort_keys=True, default=str
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        if self.disabled:
            return None
        try:
            now = time.time()
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload, expires_at FROM search_cache WHERE key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                payload_str, expires_at = row
                if expires_at < now:
                    conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                    return None
                conn.execute(
                    """
                    UPDATE search_cache
                    SET hit_count = COALESCE(hit_count, 0) + 1, last_accessed = ?
                    WHERE key = ?
                    """,
                    (now, key),
                )
                try:
                    return json.loads(payload_str)
                except Exception:
                    conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                    return None
        except Exception:
            return None

    def set(self, key: str, payload: dict[str, Any], ttl: float | None = None) -> None:
        if self.disabled:
            return
        with suppress(Exception), self._get_conn() as conn:
            now = time.time()
            expires = now + (ttl if ttl is not None else self.default_ttl)
            payload_str = json.dumps(payload, ensure_ascii=False)
            conn.execute(
                """
                INSERT OR REPLACE INTO search_cache
                    (key, created_at, expires_at, payload, hit_count, last_accessed)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (key, now, expires, payload_str, now),
            )
            self._prune_if_needed(conn)

    def _sum_payload_bytes(self, conn: sqlite3.Connection) -> int:
        # LENGTH(CAST(... AS BLOB)) counts bytes; plain LENGTH(TEXT) counts
        # characters, which undercounts multi-byte UTF-8 (emoji, CJK) by up to 4x.
        return int(
            conn.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))), 0) FROM search_cache"
            ).fetchone()[0]
        )

    def _prune_if_needed(self, conn: sqlite3.Connection) -> None:
        if self.max_rows < 1 or self.prune_batch < 1:
            return
        count = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
        overflow = count - self.max_rows
        if overflow > 0:
            limit = min(self.prune_batch, overflow)
            # Nested subquery: SQLite cannot DELETE FROM a table while the same
            # table is used in a plain IN-select with ORDER BY/LIMIT.
            conn.execute(
                """
                DELETE FROM search_cache WHERE key IN (
                    SELECT key FROM (
                        SELECT key FROM search_cache
                        ORDER BY expires_at ASC, hit_count ASC
                        LIMIT ?
                    )
                )
                """,
                (limit,),
            )
        if self.max_bytes and self.max_bytes > 0:
            total = self._sum_payload_bytes(conn)
            while total > self.max_bytes:
                conn.execute(
                    """
                    DELETE FROM search_cache WHERE key IN (
                        SELECT key FROM (
                            SELECT key FROM search_cache
                            ORDER BY expires_at ASC, hit_count ASC
                            LIMIT ?
                        )
                    )
                    """,
                    (self.prune_batch,),
                )
                new_total = self._sum_payload_bytes(conn)
                if new_total >= total:
                    break
                total = new_total

    def stats(self) -> dict[str, int]:
        """Aggregate cache telemetry: row count, payload bytes, and cumulative hits."""
        if self.disabled:
            return {"rows": 0, "bytes": 0, "hits": 0}
        try:
            with self._get_conn() as conn:
                rows, payload_bytes, hits = conn.execute(
                    """
                    SELECT COUNT(*),
                           COALESCE(SUM(LENGTH(CAST(payload AS BLOB))), 0),
                           COALESCE(SUM(COALESCE(hit_count, 0)), 0)
                    FROM search_cache
                    """
                ).fetchone()
                return {"rows": int(rows), "bytes": int(payload_bytes), "hits": int(hits)}
        except Exception:
            return {"rows": 0, "bytes": 0, "hits": 0}

    def clear(self) -> None:
        if self.disabled:
            return
        with suppress(Exception), self._get_conn() as conn:
            conn.execute("DELETE FROM search_cache;")

    def delete(self, key: str) -> None:
        """Evict a specific cache entry (e.g. on corruption or invalidation)."""
        if self.disabled:
            return
        with suppress(Exception), self._get_conn() as conn:
            conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))


_FETCH_ONLY_FIELDS = ("content", "extraction_method", "fetched_url", "fetched_at", "fetch_error")

# Freshness ceilings (seconds): the OLDEST a cached result may be when served.
# The effective TTL is min(configured default, ceiling) — the rule can only ever
# shorten, so a caller-configured shorter cache_ttl always wins.
CACHE_TTL_MAX_DAY = 1800.0    # timelimit=d: "today's news" must not be hours old
CACHE_TTL_MAX_WEEK = 7200.0   # timelimit=w
CACHE_TTL_MAX_NEWS = 3600.0   # category=news without timelimit


def _ttl_for(category: str, timelimit: str | None, default: float) -> float:
    if timelimit == "d":
        return min(default, CACHE_TTL_MAX_DAY)
    if timelimit == "w":
        return min(default, CACHE_TTL_MAX_WEEK)
    if category == "news":
        return min(default, CACHE_TTL_MAX_NEWS)
    return default


def _stamp_fetch_stats(run: SearchRun, *, requested: bool) -> None:
    """Record fetch-phase counters on ``run`` after optional ``fetch_content``."""
    run.fetch_requested = requested
    if not requested:
        run.fetch_attempted = 0
        run.fetch_ok = 0
        run.fetch_failed = 0
        return
    run.fetch_attempted = len(run.results)
    run.fetch_ok = sum(1 for item in run.results if (item.content or "").strip())
    run.fetch_failed = run.fetch_attempted - run.fetch_ok


def _strip_fetch_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Blank per-result fetch fields so cached payloads stay small (SERPs, not page text).

    Mutates ``payload`` in place; callers pass a freshly built ``run.dict()``.
    Keys stay present as ``null`` so the schema contract is stable. Fetch-phase
    counters are reset to skipped: the cache identity is SERP-only, and a later
    ``fetch=True`` hit rehydrates pages without repeating provider fanout.
    """
    for result in payload.get("results", []):
        for field_name in _FETCH_ONLY_FIELDS:
            result[field_name] = None
    payload["fetch"] = {
        "requested": False,
        "attempted": 0,
        "ok": 0,
        "failed": 0,
        "status": "skipped",
    }
    return payload


def _search_run_from_dict(payload: dict[str, Any]) -> SearchRun:
    if not isinstance(payload, dict):
        raise ValueError("Invalid payload: expected dict")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []
    results: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        engine_ranks_raw = item.get("engine_ranks")
        engine_ranks: dict[str, int] = {}
        if isinstance(engine_ranks_raw, dict):
            for k, v in engine_ranks_raw.items():
                try:
                    engine_ranks[str(k)] = int(v)
                except (ValueError, TypeError):
                    continue
        rank_contribs_raw = item.get("rank_contributions")
        rank_contributions: dict[str, float] = {}
        if isinstance(rank_contribs_raw, dict):
            for k, v in rank_contribs_raw.items():
                try:
                    contrib = float(v)
                except (ValueError, TypeError):
                    continue
                if math.isfinite(contrib):
                    rank_contributions[str(k)] = contrib
        score_weights_raw = item.get("score_weights")
        score_weights: dict[str, float] = {}
        if isinstance(score_weights_raw, dict):
            for k, v in score_weights_raw.items():
                try:
                    weight = float(v)
                except (ValueError, TypeError):
                    continue
                if math.isfinite(weight):
                    score_weights[str(k)] = weight
        try:
            score = float(item.get("score", 0.0))
            if not math.isfinite(score):
                score = 0.0
        except (ValueError, TypeError):
            score = 0.0
        raw_sources = item.get("sources")
        if isinstance(raw_sources, list):
            sources = [str(s) for s in raw_sources]
        elif isinstance(raw_sources, str):
            sources = [raw_sources]
        else:
            sources = []
        results.append(
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("snippet") or ""),
                sources=sources,
                published_at=item.get("published_at"),
                publisher=item.get("publisher"),
                score=score,
                content=item.get("content"),
                extraction_method=item.get("extraction_method"),
                fetched_url=item.get("fetched_url"),
                fetched_at=item.get("fetched_at"),
                fetch_error=item.get("fetch_error"),
                engine_ranks=engine_ranks,
                rank_contributions=rank_contributions,
                score_weights=score_weights,
            )
        )
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list):
        raw_providers = []
    providers: list[ProviderStatus] = []
    for p in raw_providers:
        if not isinstance(p, dict):
            continue
        raw_ok = p.get("ok", True)
        if isinstance(raw_ok, str):
            ok = raw_ok.strip().lower() not in ("false", "0", "no", "")
        else:
            ok = bool(raw_ok)
        try:
            rc = int(p.get("result_count", 0))
        except (ValueError, TypeError):
            rc = 0
        try:
            lat = float(p["latency_ms"]) if p.get("latency_ms") is not None else None
            if lat is not None and not math.isfinite(lat):
                lat = None
        except (ValueError, TypeError):
            lat = None
        try:
            fails = int(p["failures"]) if p.get("failures") is not None else None
        except (ValueError, TypeError):
            fails = None
        try:
            bo = float(p["backoff_remaining"]) if p.get("backoff_remaining") is not None else None
            if bo is not None and not math.isfinite(bo):
                bo = None
        except (ValueError, TypeError):
            bo = None
        cs = p.get("circuit_state")
        if cs not in ("closed", "open", "half-open"):
            cs = "closed" if cs is not None else None
        providers.append(
            ProviderStatus(
                provider=str(p.get("provider", "")),
                query=str(p.get("query", "")),
                ok=ok,
                result_count=rc,
                error=p.get("error"),
                error_kind=p.get("error_kind"),
                latency_ms=lat,
                circuit_state=cs,
                failures=fails,
                backoff_remaining=bo,
            )
        )

    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _as_opt_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    fetch_raw = payload.get("fetch")
    if not isinstance(fetch_raw, dict):
        fetch_raw = {}
    return SearchRun(
        query=str(payload.get("query", "")),
        results=results,
        providers=providers,
        cached=bool(payload.get("cached", False)),
        run_at=payload.get("run_at"),
        query_variant_count=_as_opt_int(payload.get("query_variant_count")),
        request_count=_as_opt_int(payload.get("request_count")),
        fetch_requested=bool(fetch_raw.get("requested", False)),
        fetch_attempted=_as_int(fetch_raw.get("attempted")),
        fetch_ok=_as_int(fetch_raw.get("ok")),
        fetch_failed=_as_int(fetch_raw.get("failed")),
    )
