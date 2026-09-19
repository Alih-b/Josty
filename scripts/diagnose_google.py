"""Opt-in live comparison for #60; stdout is JSONL, no files are overwritten.

Run with the project installed: python scripts/diagnose_google.py > google.jsonl
Each arm gets a fresh process so importing Josty cannot contaminate raw ddgs.
Private ddgs hooks are diagnostic instrumentation, not production dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Arm:
    """Explicit per-arm behaviour, so the arm name is never parsed as a command."""

    kind: str  # "raw" is a bare DDGS call; "josty" goes through the wrapper
    concurrency: int = 6
    import_josty: bool = False  # raw-registered imports josty for its google registration


ARMS = {
    "raw-default": Arm(kind="raw"),
    "raw-registered": Arm(kind="raw", import_josty=True),
    "josty-sequential": Arm(kind="josty", concurrency=1, import_josty=True),
    "josty-fanout": Arm(kind="josty", import_josty=True),
}


def probe(query: str, arm: str) -> dict:
    from ddgs.ddgs import DDGS
    from ddgs.engines import ENGINES
    from ddgs.http_client import HttpClient

    spec = ARMS[arm]
    registered_before = "google" in ENGINES["text"]
    if spec.import_josty:
        from josty import Josty

    selected = []
    http = []
    original_engines = DDGS._get_engines
    original_request = HttpClient.request

    def trace_engines(self, category, backend):
        engines = original_engines(self, category, backend)
        selected.append({"requested": backend, "selected": [e.name for e in engines]})
        return engines

    def trace_request(self, method, url, *args, **kwargs):
        start = time.perf_counter()
        event = {"host": urlsplit(url).hostname, "http_status": None}
        try:
            response = original_request(self, method, url, *args, **kwargs)
            event["http_status"] = response.status_code
            return response
        except Exception as exc:
            event["exception"] = type(exc).__name__
            raise
        finally:
            event["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
            http.append(event)

    DDGS._get_engines = trace_engines
    HttpClient.request = trace_request
    record = {
        "arm": arm,
        "query": query,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ddgs_version": version("ddgs"),
        "google_registered_before": registered_before,
        "google_registered_after": "google" in ENGINES["text"],
        "selected_engines": selected,
        "http": http,
    }
    start = time.perf_counter()
    try:
        if spec.kind == "raw":
            rows = DDGS(timeout=8).text(
                query, backend="google", max_results=10, safesearch="moderate"
            )
            record.update(count=len(rows), ok=True)
        else:
            engine = Josty(
                timeout=8, enable_cache=False,
                max_search_concurrency=spec.concurrency,
            )
            run = asyncio.run(engine.search_run(query, limit=10))
            google = next(p for p in run.providers if p.provider == "google")
            record.update(
                count=google.result_count, ok=google.ok,
                error_kind=google.error_kind, status=run.status,
                providers=[p.dict() for p in run.providers],
            )
    except Exception as exc:
        record.update(count=0, ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        record["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
        DDGS._get_engines = original_engines
        HttpClient.request = original_request
    return record


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queries", nargs="*", default=["Python 3.13 release notes whatsnew"])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=60)
    parser.add_argument("--arm", choices=sorted(ARMS), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    return args


def run_arm(query: str, arm: str) -> None:
    """Child mode: one arm, one query, one JSONL record on stdout."""
    print(json.dumps(probe(query, arm)), flush=True)


def orchestrate(args: argparse.Namespace) -> None:
    """Parent mode: re-invoke this file per (repeat, query, arm) in shuffled order."""
    rng = random.Random(args.seed)
    for repeat in range(args.repeats):
        for query in args.queries:
            arms = list(ARMS)
            rng.shuffle(arms)
            for arm in arms:
                command = [sys.executable, __file__, "--arm", arm, "--", query]
                try:
                    child = subprocess.run(
                        command, capture_output=True, text=True, timeout=90, check=True,
                    )
                    record = json.loads(child.stdout)
                except (subprocess.SubprocessError, ValueError) as exc:
                    record = {
                        "arm": arm, "query": query,
                        "harness_error": f"{type(exc).__name__}: {exc}",
                    }
                record.update(repeat=repeat + 1, seed=args.seed)
                print(json.dumps(record), flush=True)


def main() -> None:
    args = parse_args()
    if args.arm:
        run_arm(args.queries[0], args.arm)
        return
    orchestrate(args)


if __name__ == "__main__":
    main()
