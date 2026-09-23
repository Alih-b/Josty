"""Per-engine circuit breaker with sliding-window failures and backoff."""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc






class CircuitBreaker:
    """In-process per-(backend, error_class) tri-state circuit breaker.

    State lifecycle:
    - CLOSED: Normal operation. If failures in sliding window reach ``fail_threshold``,
      the circuit trips to OPEN.
    - OPEN: Calls are blocked with a cool-down timestamp. Consecutive trips apply
      exponential backoff. When cool-down expires, transitions to HALF_OPEN.
    - HALF_OPEN: Exactly one in-flight trial probe is admitted. Success resets
      to CLOSED and clears failure history and consecutive trips. Failure trips
      back to OPEN. Concurrent callers skip until the probe completes.

    ``error_class`` exists for contract compatibility: ``"rate_limit"`` and
    ``"search"`` are aliases sharing one failure namespace, and the default
    follows the default breaker contract (``"rate_limit"``).
    """

    def __init__(
        self,
        *,
        fail_threshold: int = 3,
        window_seconds: float = 60.0,
        cool_down_seconds: float = 30.0,
    ):
        if fail_threshold < 1 or window_seconds <= 0 or cool_down_seconds <= 0:
            raise ValueError("breaker thresholds must be positive")
        self.fail_threshold = int(fail_threshold)
        self.window_seconds = float(window_seconds)
        self.cool_down_seconds = float(cool_down_seconds)
        self._state: dict[tuple[str, str], str] = {}
        self._failures: dict[tuple[str, str], list[float]] = {}
        self._open_until: dict[tuple[str, str], float] = {}
        self._consecutive_trips: dict[tuple[str, str], int] = {}
        self._last_trip_at: dict[tuple[str, str], float] = {}
        self._probe_inflight: dict[tuple[str, str], bool] = {}
        self._keys_by_backend: dict[str, set[tuple[str, str]]] = {}
        self._latencies: dict[str, float] = {}
        self._lock = threading.Lock()
        self.persist_fn: Any = None

    def load_state(self, states: list[dict[str, Any]]) -> None:
        """Hydrate breaker state from persisted storage."""
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            for item in states:
                backend = item.get("backend")
                error_class = item.get("error_class", "search")
                if not backend:
                    continue
                key = self._key(backend, error_class)
                self._register_key(key)
                open_until_epoch = float(item.get("open_until", 0.0))
                remaining = open_until_epoch - now_wall
                trips = int(item.get("consecutive_trips", 0))
                if item.get("state") == "open" and remaining > 0:
                    self._state[key] = "open"
                    self._open_until[key] = now_mono + remaining
                    self._consecutive_trips[key] = trips
                    self._last_trip_at[key] = now_mono
                elif trips > 0:
                    self._consecutive_trips[key] = trips

    @staticmethod
    def _key(backend: str, error_class: str) -> tuple[str, str]:
        if error_class == "rate_limit":
            error_class = "search"
        return (backend, error_class)

    def _register_key(self, key: tuple[str, str]) -> None:
        self._keys_by_backend.setdefault(key[0], set()).add(key)

    def _cool_down_message(self, open_until: float, now: float) -> str:
        until_iso = (
            datetime.fromtimestamp(time.time() + (open_until - now), UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return f"skipped: engine in cool-down until {until_iso}"

    def _admit_half_open_locked(self, key: tuple[str, str]) -> tuple[bool, str | None]:
        """Admit a single HALF_OPEN trial probe; concurrent callers are skipped.

        Waiting here would block the asyncio event loop (status() is sync), so
        non-probe callers skip until the in-flight probe completes.
        """
        if self._probe_inflight.get(key):
            return False, "skipped: engine half-open probe in flight"
        self._probe_inflight[key] = True
        return True, None

    def _decay_trips_locked(self, key: tuple[str, str], now: float) -> int:
        """Reset consecutive trips after idle time past the last backoff window."""
        prev = self._consecutive_trips.get(key, 0)
        last = self._last_trip_at.get(key, 0.0)
        if prev and last:
            last_backoff = self.cool_down_seconds * (2 ** min(prev - 1, 6))
            if now - last > last_backoff + self.window_seconds:
                return 0
        return prev

    def _trip_locked(self, key: tuple[str, str], now: float) -> None:
        trips = self._decay_trips_locked(key, now) + 1
        self._consecutive_trips[key] = trips
        self._last_trip_at[key] = now
        backoff = self.cool_down_seconds * (2 ** min(trips - 1, 6))
        self._open_until[key] = now + backoff
        self._state[key] = "open"
        self._probe_inflight.pop(key, None)
        self._register_key(key)
        if self.persist_fn:
            with suppress(Exception):
                open_until_epoch = time.time() + backoff
                self.persist_fn(
                    key[0], key[1], "open", open_until_epoch, trips, time.time()
                )

    def status(self, backend: str, error_class: str = "rate_limit") -> tuple[bool, str | None]:
        """Return ``(allowed, skip_message)`` for a backend/error pair.

        HALF_OPEN admits exactly one in-flight trial probe. Concurrent fanout
        callers are skipped until that probe succeeds (CLOSED) or fails (OPEN).
        """
        now = time.monotonic()
        key = self._key(backend, error_class)

        with self._lock:
            self._register_key(key)
            if self._state.get(key) == "open":
                open_until = self._open_until.get(key, 0.0)
                if now < open_until:
                    return False, self._cool_down_message(open_until, now)
                self._state[key] = "half-open"
                self._failures[key] = []
                return self._admit_half_open_locked(key)

            if self._state.get(key) == "half-open":
                return self._admit_half_open_locked(key)

            return True, None

    def release_probe(self, backend: str, error_class: str = "rate_limit") -> None:
        """Drop the HALF_OPEN in-flight flag after a trial call completes."""
        key = self._key(backend, error_class)
        with self._lock:
            self._probe_inflight.pop(key, None)

    def record_failure(self, backend: str, error_class: str = "rate_limit") -> None:
        """Record a failure event for backend/error_class within sliding window."""
        now = time.monotonic()
        key = self._key(backend, error_class)

        with self._lock:
            self._register_key(key)
            # Freeze timer while open; do not extend on repeated failures during cool-down
            if self._state.get(key) == "open" and now < self._open_until.get(key, 0.0):
                return

            # If cool-down elapsed while open, transition to half-open and clear stale failures
            if self._state.get(key) == "open" and now >= self._open_until.get(key, 0.0):
                self._state[key] = "half-open"
                self._failures[key] = []

            is_half_open = self._state.get(key) == "half-open"

            events = [t for t in self._failures.get(key, []) if now - t <= self.window_seconds]
            events.append(now)
            self._failures[key] = events

            if is_half_open or len(events) >= self.fail_threshold:
                self._trip_locked(key, now)

    def record_success(self, backend: str, error_class: str = "rate_limit") -> None:
        """Record a success event, resetting circuit state to closed and clearing history."""
        key = self._key(backend, error_class)
        with self._lock:
            self._register_key(key)
            self._state[key] = "closed"
            self._failures[key] = []
            self._open_until[key] = 0.0
            self._consecutive_trips[key] = 0
            self._probe_inflight.pop(key, None)
            if self.persist_fn:
                with suppress(Exception):
                    self.persist_fn(key[0], key[1], "closed", 0.0, 0, 0.0)

    def record_latency(self, backend: str, latency_ms: float) -> None:
        """Record the most recent execution latency for a backend."""
        with self._lock:
            self._latencies[backend] = float(latency_ms)

    def get_state(self, backend: str) -> dict[str, Any]:
        """Return circuit-breaker telemetry for a backend without mutating state.

        An expired OPEN cool-down is reported as ``half-open``. The OPEN →
        HALF_OPEN transition itself happens on ``status()`` / ``record_failure``.
        """
        now = time.monotonic()
        with self._lock:
            matching_keys = list(self._keys_by_backend.get(backend, ()))
            if not matching_keys:
                matching_keys = [k for k in self._state if k[0] == backend]
                if not matching_keys:
                    matching_keys = [k for k in self._failures if k[0] == backend]

            state = "closed"
            backoff_remaining = 0.0
            failures = 0
            has_half_open = False

            for key in matching_keys:
                stored = self._state.get(key, "closed")
                open_until = self._open_until.get(key, 0.0)
                if stored == "open":
                    remaining = open_until - now
                    if remaining > 0:
                        state = "open"
                        if remaining > backoff_remaining:
                            backoff_remaining = remaining
                        active = [
                            t
                            for t in self._failures.get(key, [])
                            if now - t <= self.window_seconds
                        ]
                        failures = max(failures, len(active))
                    else:
                        has_half_open = True
                elif stored == "half-open":
                    has_half_open = True
                    active = [
                        t for t in self._failures.get(key, []) if now - t <= self.window_seconds
                    ]
                    failures = max(failures, len(active))
                else:
                    active = [
                        t for t in self._failures.get(key, []) if now - t <= self.window_seconds
                    ]
                    failures = max(failures, len(active))

            if state != "open" and has_half_open:
                state = "half-open"

            return {
                "state": state,
                "failures": failures,
                "backoff_remaining": round(max(0.0, backoff_remaining), 2),
                "last_latency_ms": self._latencies.get(backend),
            }
