#!/usr/bin/env python3
"""Cause-isolated follow-ups. Sequential on purpose: cache keys and empty-branch
reproducibility cannot be inferred from a mixed concurrent campaign."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
RAW.mkdir(parents=True, exist_ok=True)

CACHE_QUERY = "josty cache isolation probe perpetual funding rate"
LIMIT_QUERY = "crypto futures trading"
EMPTY_QUERY = "crypto spot exchange matching engine limit market order execution"


def run(job_id: str, args: list[str]) -> dict:
    stdout_path = RAW / f"{job_id}.json"
    stderr_path = RAW / f"{job_id}.err"
    started = time.time()
    proc = subprocess.run(
        ["uvx", "josty", *args],
        capture_output=True,
        text=True,
        timeout=180,
    )
    elapsed = time.time() - started
    stderr_path.write_text(proc.stderr or "")
    stdout_path.write_text(proc.stdout or "")
    meta: dict = {
        "id": job_id,
        "args": args,
        "returncode": proc.returncode,
        "elapsed_s": round(elapsed, 3),
        "stdout_bytes": len(proc.stdout or ""),
        "stderr_bytes": len(proc.stderr or ""),
        "stderr_head": (proc.stderr or "")[:400],
    }
    try:
        payload = json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        meta["parse_error"] = True
        meta["stdout_head"] = (proc.stdout or "")[:400]
        print(json.dumps({k: meta[k] for k in ("id", "returncode", "elapsed_s")}), flush=True)
        return meta
    if isinstance(payload, dict):
        meta["status"] = payload.get("status")
        meta["schema_version"] = payload.get("schema_version")
        meta["partial"] = payload.get("partial")
        meta["cached"] = payload.get("cached")
        meta["count_field"] = payload.get("count")
        meta["result_count"] = len(payload.get("results") or [])
        meta["error"] = payload.get("error")
        meta["providers"] = [
            {
                "provider": p.get("provider"),
                "ok": p.get("ok"),
                "error_kind": p.get("error_kind"),
                "result_count": p.get("result_count"),
                "error": p.get("error"),
            }
            for p in (payload.get("providers") or [])
        ]
    print(
        json.dumps(
            {
                k: meta.get(k)
                for k in (
                    "id",
                    "returncode",
                    "elapsed_s",
                    "status",
                    "result_count",
                    "cached",
                )
            }
        ),
        flush=True,
    )
    return meta


def main() -> int:
    metas: list[dict] = []

    # CLI bound: limit 101 must fail like sixth --site (ValueError -> exit 2).
    metas.append(run("39-limit-101", [LIMIT_QUERY, "--limit", "101"]))

    # Fused-count vs requested limit: same query as 24, twice, --no-cache.
    metas.append(
        run(
            "40-limit-100-a",
            [LIMIT_QUERY, "--limit", "100", "--search-concurrency", "16", "--no-cache"],
        )
    )
    metas.append(
        run(
            "40-limit-100-b",
            [LIMIT_QUERY, "--limit", "100", "--search-concurrency", "16", "--no-cache"],
        )
    )

    # Empty complete reproducibility: exact 02 query, twice, --no-cache.
    metas.append(
        run(
            "41-empty-a",
            [EMPTY_QUERY, "--limit", "15", "--profile", "academic", "--no-cache"],
        )
    )
    metas.append(
        run(
            "41-empty-b",
            [EMPTY_QUERY, "--limit", "15", "--profile", "academic", "--no-cache"],
        )
    )

    # Cache isolation: unique query, populate, hit, --no-cache bypass, hit again.
    metas.append(run("42-cache-clear", ["--clear-cache"]))
    metas.append(run("43-cache-miss", [CACHE_QUERY, "--limit", "10"]))
    metas.append(run("44-cache-hit-a", [CACHE_QUERY, "--limit", "10"]))
    metas.append(run("45-cache-nocache", [CACHE_QUERY, "--limit", "10", "--no-cache"]))
    metas.append(run("46-cache-hit-b", [CACHE_QUERY, "--limit", "10"]))

    # Confound reconstruction: --no-cache then same args without it must miss
    # (first call does not write), then a third call can hit if n>0.
    confound_q = "josty nocache confound probe isolated margin"
    metas.append(run("47-confound-nocache", [confound_q, "--limit", "8", "--no-cache"]))
    metas.append(run("48-confound-second", [confound_q, "--limit", "8"]))
    metas.append(run("49-confound-third", [confound_q, "--limit", "8"]))

    (ROOT / "isolation_meta.json").write_text(json.dumps(metas, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
