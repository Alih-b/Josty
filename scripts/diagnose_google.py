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
from datetime import datetime, timezone
from importlib.metadata import version
from urllib.parse import urlsplit

ARMS = ("raw-default", "raw-registered", "josty-sequential", "josty-fanout")


def probe(query: str, arm: str) -> dict:
    from ddgs.ddgs import DDGS
    from ddgs.engines import ENGINES
    from ddgs.http_client import HttpClient

    registered_before = "google" in ENGINES["text"]
    if arm != "raw-default":
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
        if arm.startswith("raw-"):
            rows = DDGS(timeout=8).text(
                query, backend="google", max_results=10, safesearch="moderate"
            )
            record.update(count=len(rows), ok=True)
        else:
            engine = Josty(
                timeout=8, enable_cache=False,
                max_search_concurrency=1 if arm == "josty-sequential" else 6,
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queries", nargs="*", default=["Python 3.13 release notes whatsnew"])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=60)
    parser.add_argument("--arm", choices=ARMS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.arm:
        print(json.dumps(probe(args.queries[0], args.arm)), flush=True)
        return
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


if __name__ == "__main__":
    main()
