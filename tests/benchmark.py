"""Canonical live benchmark for Deep Search.

Runs 20 fixed factual queries against three runners:
  - deep_search  : the project's async wrapper
  - raw_ddgs     : direct DDGS client, serial per backend
  - websearch_skill : the websearch-skill CLI subprocess

Each query is repeated 8 times with rotating backend groups so no single
upstream engine sees back-to-back traffic. Results are streamed to a
per-run JSONL and can be replayed/scored offline by `benchmark_replay.py`.

Outputs (per run):
  <run_id>/results.jsonl       per (runner, query, repeat) row
  <run_id>/per_query.csv       wide per-(query, runner) table
  <run_id>/summary.json        aggregates + failure telemetry
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / ".agents" / "skills" / "josty" / "src"
sys.path.insert(0, str(SRC))

from benchmark_grade import grade_run  # noqa: E402
from benchmark_metrics import (  # noqa: E402
    graded_recall_at_k,
    mrr_one,
    ndcg_at_k,
    precision_at_k,
)
from benchmark_queries import QUERIES  # noqa: E402
from ddgs import DDGS  # type: ignore  # noqa: E402
from josty.engine import Josty, canonical  # noqa: E402

OUT_ROOT = HERE / "benchmark_out"
OUT_ROOT.mkdir(exist_ok=True)
RUN_ID = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
RUN_DIR = OUT_ROOT / RUN_ID
RUN_DIR.mkdir(parents=True, exist_ok=True)

LIMIT = 10
TIMEOUT = 8
REPEATS = 8
INTER_QUERY_SLEEP_S = 4.0
INTER_REPEAT_SLEEP_S = 1.5
INTER_RUNNER_SLEEP_S = 0.5
WEBSEARCH_BIN = "websearch"

# Rotation: 3 backend groups, cycled per query slot, so each upstream
# engine is hit roughly every 3rd query, not back-to-back.
ROTATION_SLOTS = (
    "bing,brave,duckduckgo",
    "google,mojeek,startpage",
    "yandex,yahoo",
)


def backends_for_slot(slot_index: int) -> tuple[str, ...]:
    return (ROTATION_SLOTS[slot_index % len(ROTATION_SLOTS)],)


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------


async def run_deep_search(query: str, backends: tuple[str, ...]) -> tuple[list[dict], float]:
    engine = Josty(timeout=TIMEOUT, backends=backends)
    started = time.perf_counter()
    run = await engine.search_run(query, limit=LIMIT)
    wall = time.perf_counter() - started
    flat = [{"title": r.title, "snippet": r.snippet, "url": r.url} for r in run.results]
    return flat, wall


def run_raw_ddgs(query: str, backends: tuple[str, ...]) -> tuple[list[dict], float]:
    started = time.perf_counter()
    flat: list[dict] = []
    for group in backends:
        for backend in (b.strip() for b in group.split(",") if b.strip()):
            try:
                ddgs = DDGS(timeout=TIMEOUT)
                rows = list(
                    ddgs.text(
                        query,
                        backend=backend,
                        max_results=LIMIT,
                        safesearch="moderate",
                    )
                )
                for row in rows:
                    flat.append(
                        {
                            "title": row.get("title", ""),
                            "snippet": row.get("body", ""),
                            "url": row.get("href") or row.get("url") or "",
                        }
                    )
            except Exception:
                pass
    wall = time.perf_counter() - started
    return flat, wall


def run_websearch_skill(query: str, backends: tuple[str, ...]) -> tuple[list[dict], float]:
    """Subprocess websearch search --engines ddgs --json, with the rotated
    backend group passed via --ddgs-backends so each query slot uses a
    different set of engines."""
    started = time.perf_counter()
    backends_csv = ",".join(
        b.strip() for group in backends for b in group.split(",") if b.strip()
    )
    cmd = [
        WEBSEARCH_BIN,
        "search",
        query,
        "--engines",
        "ddgs",
        "--ddgs-backends",
        backends_csv,
        "--json",
        "--count",
        str(LIMIT),
        "--max-results",
        str(LIMIT),
        "--safesearch",
        "moderate",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT * 3)
    except FileNotFoundError:
        return [], time.perf_counter() - started
    except subprocess.TimeoutExpired:
        return [], time.perf_counter() - started
    wall = time.perf_counter() - started
    if proc.returncode != 0 or not proc.stdout.strip():
        return [], wall
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [], wall
    data = envelope.get("data") or {}
    raw_results = data.get("results") or []
    flat = [
        {
            "title": (r.get("title") or ""),
            "snippet": (r.get("snippet") or r.get("description") or ""),
            "url": (r.get("url") or r.get("href") or ""),
        }
        for r in raw_results
    ]
    return flat, wall


RUNNERS: dict[str, Callable] = {
    "deep_search": run_deep_search,
    "raw_ddgs": run_raw_ddgs,
    "websearch_skill": run_websearch_skill,
}


async def _run_async(
    fn: Callable, query: str, backends: tuple[str, ...]
) -> tuple[list[dict], float]:
    if asyncio.iscoroutinefunction(fn):
        return await fn(query, backends)
    return fn(query, backends)


# --------------------------------------------------------------------------
# Metric computation
# --------------------------------------------------------------------------


def evaluate_query(spec: dict, results: list[dict]) -> dict:
    rels = grade_run(spec, results)
    ideal_total = sum(sorted(rels, reverse=True)) or 0
    return {
        "ndcg_at_10": round(ndcg_at_k(rels, 10), 4),
        "mrr": round(mrr_one(rels), 4),
        "precision_at_5": round(precision_at_k(rels, 5), 4),
        "graded_recall_at_10": round(graded_recall_at_k(rels, ideal_total, 10), 4),
        "rels": rels,
        "result_count": len(results),
    }


def _count_unique(results: list[dict]) -> int:
    keys: list[str] = []
    for r in results:
        with suppress(ValueError):
            keys.append(canonical(r.get("url", "")))
    return len(set(keys))


# --------------------------------------------------------------------------
# Per-(runner, query) row
# --------------------------------------------------------------------------


async def benchmark_one(
    spec: dict,
    runner_name: str,
    runner_fn: Callable,
    run_idx: int,
    backends: tuple[str, ...],
) -> dict:
    started = time.perf_counter()
    error: str | None = None
    try:
        results, _ = await _run_async(runner_fn, spec["query"], backends)
    except BaseException as exc:  # noqa: BLE001
        results = []
        error = f"{type(exc).__name__}: {exc}"
    wall = round(time.perf_counter() - started, 3)
    metrics = evaluate_query(spec, results)
    return {
        "runner": runner_name,
        "query_id": spec["id"],
        "run_idx": run_idx,
        "slot_backends": list(backends),
        "wall_clock_seconds": wall,
        "result_count": metrics["result_count"],
        "ndcg_at_10": metrics["ndcg_at_10"],
        "mrr": metrics["mrr"],
        "precision_at_5": metrics["precision_at_5"],
        "graded_recall_at_10": metrics["graded_recall_at_10"],
        "unique_post_canonical": _count_unique(results),
        "rels": metrics["rels"],
        "error": error,
    }


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


async def main() -> None:
    print(
        f"[{RUN_ID}] benchmark: {len(QUERIES)} queries × {REPEATS} repeats × "
        f"{len(RUNNERS)} runners (with backend rotation)"
    )
    print(f"Output: {RUN_DIR}")

    jsonl_path = RUN_DIR / "results.jsonl"
    jsonl_f = jsonl_path.open("w", encoding="utf-8")
    all_rows: list[dict] = []

    # Outer loop: queries. Inner loop: repeats. Inner-inner: runners.
    for qi, spec in enumerate(QUERIES):
        backends = backends_for_slot(qi)
        for repeat in range(REPEATS):
            line = [f"  [{spec['id']:>20} r{repeat} slot={backends[0][:14]}…]"]
            for name, fn in RUNNERS.items():
                try:
                    row = await benchmark_one(spec, name, fn, repeat, backends)
                except BaseException as exc:  # noqa: BLE001
                    row = {
                        "runner": name,
                        "query_id": spec["id"],
                        "run_idx": repeat,
                        "slot_backends": list(backends),
                        "wall_clock_seconds": 0.0,
                        "result_count": 0,
                        "ndcg_at_10": 0.0,
                        "mrr": 0.0,
                        "precision_at_5": 0.0,
                        "graded_recall_at_10": 0.0,
                        "unique_post_canonical": 0,
                        "rels": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                all_rows.append(row)
                jsonl_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                jsonl_f.flush()
                line.append(
                    f"{name}=({row['ndcg_at_10']:.2f}/{row['mrr']:.2f} "
                    f"{row['wall_clock_seconds']:.1f}s u{row['unique_post_canonical']})"
                )
                await asyncio.sleep(INTER_RUNNER_SLEEP_S)
            print(" ".join(line), flush=True)
            await asyncio.sleep(INTER_REPEAT_SLEEP_S)
        # Sleep between queries to let upstream rate limits reset.
        await asyncio.sleep(INTER_QUERY_SLEEP_S)

    jsonl_f.close()

    # Per-runner aggregates
    per_runner = _aggregate(all_rows)
    with (RUN_DIR / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(per_runner, f, indent=2, ensure_ascii=False)

    # Wide CSV
    csv_path = RUN_DIR / "per_query.csv"
    import csv as _csv

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        if all_rows:
            writer = _csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)

    print("\nPer-runner aggregates:")
    print(json.dumps(per_runner, indent=2, ensure_ascii=False))
    print(f"\nWrote {jsonl_path}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {RUN_DIR / 'summary.json'}")


def _aggregate(rows: list[dict]) -> dict:
    by_runner: dict[str, list[dict]] = {}
    for r in rows:
        by_runner.setdefault(r["runner"], []).append(r)
    out: dict[str, dict] = {}
    for runner, rrows in by_runner.items():
        successful = [r for r in rrows if r["result_count"] > 0]
        out[runner] = {
            "queries": len({r["query_id"] for r in rrows}),
            "runs": len(rrows),
            "empty_runs": sum(1 for r in rrows if r["result_count"] == 0),
            "error_runs": sum(1 for r in rrows if r.get("error")),
            "all_runs": _stats_view(rrows),
            "successful_runs_only": _stats_view(successful),
        }
    return out


def _stats_view(rrows: list[dict]) -> dict:
    keys = ["ndcg_at_10", "mrr", "precision_at_5", "graded_recall_at_10"]
    agg: dict[str, list[float]] = {k: [] for k in keys}
    agg["wall_clock_seconds"] = []
    agg["result_count"] = []
    agg["unique_post_canonical"] = []
    for r in rrows:
        for k in keys:
            agg[k].append(float(r.get(k, 0.0)))
        agg["wall_clock_seconds"].append(float(r["wall_clock_seconds"]))
        agg["result_count"].append(int(r["result_count"]))
        agg["unique_post_canonical"].append(int(r["unique_post_canonical"]))
    return {k: _describe(v) for k, v in agg.items()}


def _describe(values: list[float]) -> dict:
    if not values:
        return {"mean": 0.0, "std": 0.0, "median": 0.0, "n": 0}
    mean = statistics.fmean(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    median = statistics.median(values)
    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "median": round(median, 4),
        "n": len(values),
    }


if __name__ == "__main__":
    asyncio.run(main())
