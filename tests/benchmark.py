"""Live status-truth capture for Josty (#103).

Yield depends on the egress and on what each engine does that minute, so this
benchmark no longer compares live yield against another tool. It measures
whether the statuses Josty reports are true.

For every (query, repeat) it runs one search and records, per engine, what the
reported error_kind said next to what the wire said at capture time:
http_statuses_all, body_bytes, page_title and zero_result_kind. The two
disagreeing is a mislabel, and the mislabel rate is the number this benchmark
exists for. A run that returns nothing is a data point, not a failed run.

Two kinds of row. "ranking" rows come only from the sources whose answers are
stable across egresses (two-egress Jaccard of at least 0.9: mwmbl, the
Wikipedia list=search API and the GitHub API) and double as ranking fixtures.
"truth" rows come from the scrapers: their outcomes are static across egresses,
their yield is noise, so they are measured for status truth and nothing else.
The classification itself is pinned offline by tests/test_status_matrix.py.

Usage: python tests/benchmark.py [repeats]

Outputs (per run):
  <run_id>/results.jsonl   per (query, repeat) row with its truth fields
  <run_id>/per_query.csv   wide per-query table
  <run_id>/summary.json    ranking aggregates + the mislabel rate
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import re
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

from benchmark_grade import grade_run  # noqa: E402
from benchmark_metrics import (  # noqa: E402
    graded_recall_at_k,
    mrr_one,
    ndcg_at_k,
    precision_at_k,
)
from benchmark_queries import QUERIES  # noqa: E402

from josty.engine import Josty  # noqa: E402
from josty.errors import (  # noqa: E402
    _BLOCKED_HTTP_STATUSES,
    _PARSE_BODY_BYTES,
    _is_challenge_page,
)
from josty.models import SearchRun  # noqa: E402
from josty.providers import _WireTap  # noqa: E402
from josty.ranking import canonical  # noqa: E402

OUT_ROOT = HERE / "benchmark_out"
RUN_ID = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
RUN_DIR = OUT_ROOT / RUN_ID
LIMIT = 10
TIMEOUT = 8
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
INTER_QUERY_SLEEP_S = 4.0
INTER_REPEAT_SLEEP_S = 1.5

# Ranking fixtures: answers stable across egresses (two-egress Jaccard >= 0.9).
STABLE_SLOTS = ("mwmbl", "wikipedia", "github-api")
# Status truth only: outcomes are static, yield is egress noise.
SCRAPER_SLOTS = ("brave,duckduckgo", "mojeek,startpage", "yahoo", "bing,yandex")


# --------------------------------------------------------------------------
# Capture: what the wire said, recorded beside what Josty reported
# --------------------------------------------------------------------------

CAPTURE: list[dict] = []
_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)
_ORIGINAL_RECORDING = _WireTap._recording


def _host_of(url: object) -> str | None:
    return urlsplit(str(url)).hostname if url else None


def install_capture() -> None:
    """See every response the adapter's tap sees, without changing its verdict."""

    def recording(self, request):
        tapped = _ORIGINAL_RECORDING(self, request)

        def capturing(*args, **kwargs):
            url = args[1] if len(args) > 1 else kwargs.get("url")
            try:
                response = tapped(*args, **kwargs)
            except Exception as exc:
                CAPTURE.append(
                    {
                        "host": _host_of(url),
                        "http_status": None,
                        "body_bytes": 0,
                        "page_title": None,
                        "challenge": False,
                        "transport_error": type(exc).__name__,
                    }
                )
                raise
            text = getattr(response, "text", "")
            text = text if isinstance(text, str) else ""
            size = getattr(response, "content", b"")
            status = getattr(response, "status_code", None)
            title = _TITLE_RE.search(text)
            CAPTURE.append(
                {
                    "host": _host_of(url),
                    "http_status": status if isinstance(status, int) else None,
                    "body_bytes": (
                        len(size) if isinstance(size, (bytes, bytearray)) else len(text)
                    ),
                    "page_title": title.group(1).strip()[:120] if title else None,
                    "challenge": bool(text) and _is_challenge_page(text),
                }
            )
            return response

        return capturing

    _WireTap._recording = recording


def _expected_kind(facts: list[dict]) -> str | None:
    """What a zero-row engine must report, judged from the capture alone."""
    if not facts:
        return None  # nothing observed (native source, no request): no verdict
    codes = {fact["http_status"] for fact in facts}
    if None in codes:
        return None  # a transport failure is filtered or network; not this oracle's call
    if 429 in codes:
        return "rate_limited"
    if codes & _BLOCKED_HTTP_STATUSES:
        return "blocked"
    if any(500 <= code < 600 for code in codes):
        return "network"
    if codes - {200}:
        return "parse"
    if any(fact["challenge"] for fact in facts):
        return "blocked"
    if max(fact["body_bytes"] for fact in facts) > _PARSE_BODY_BYTES:
        return "parse"
    return "empty"


def _zero_result_kind(facts: list[dict]) -> str | None:
    """The capture's own name for why an engine returned no rows."""
    if not facts:
        return None
    codes = [fact["http_status"] for fact in facts]
    if None in codes:
        return "no_response"
    refused = [code for code in codes if code != 200]
    if refused:
        return f"http_{refused[0]}"
    if any(fact["challenge"] for fact in facts):
        return "challenge_page"
    if max(fact["body_bytes"] for fact in facts) > _PARSE_BODY_BYTES:
        return "large_unparsed"
    return "small_page"


def truth_of(run: SearchRun, seen: list[dict]) -> dict:
    """Per-engine capture facts beside the reported kind, and the mislabel count."""
    providers: list[dict] = []
    judged = mislabels = 0
    for status in run.providers:
        # The listed host or a subdomain of it: ddgs's DuckDuckGo text engine
        # asks html.duckduckgo.com.
        host = Josty.BACKEND_HOSTS.get(status.provider, "")
        facts = [
            fact
            for fact in seen
            if host and (fact["host"] == host or str(fact["host"]).endswith("." + host))
        ]
        largest = max(facts, key=lambda fact: fact["body_bytes"], default=None)
        zero_rows = status.result_count == 0
        expected = _expected_kind(facts) if zero_rows else None
        if expected is not None:
            judged += 1
            mislabels += status.error_kind != expected
        providers.append(
            {
                "provider": status.provider,
                "ok": status.ok,
                "error_kind": status.error_kind,
                "result_count": status.result_count,
                "reported_http_status": status.http_status,
                "refusal_rate": status.refusal_rate,
                "http_statuses_all": [fact["http_status"] for fact in facts] or None,
                "body_bytes": largest["body_bytes"] if largest else None,
                "page_title": largest["page_title"] if largest else None,
                "zero_result_kind": _zero_result_kind(facts) if zero_rows else None,
                "expected_kind": expected,
            }
        )
    return {
        "providers": providers,
        "judged": judged,
        "mislabels": mislabels,
        "mislabel": (mislabels > 0) if judged else None,
    }


# --------------------------------------------------------------------------
# Metric computation (ranking fixtures; stable sources only)
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
    for item in results:
        try:
            keys.append(canonical(item.get("url", "")))
        except ValueError:
            continue
    return len(set(keys))


# --------------------------------------------------------------------------
# One (query, repeat) row
# --------------------------------------------------------------------------


async def benchmark_one(
    spec: dict, run_idx: int, backends: tuple[str, ...], kind: str
) -> dict:
    started = time.perf_counter()
    mark = len(CAPTURE)
    error: str | None = None
    run: SearchRun | None = None
    try:
        engine = Josty(timeout=TIMEOUT, backends=backends, enable_cache=False)
        run = await engine.search_run(spec["query"], limit=LIMIT)
    except BaseException as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    wall = round(time.perf_counter() - started, 3)
    results = (
        [{"title": r.title, "snippet": r.snippet, "url": r.url} for r in run.results]
        if run is not None
        else []
    )
    metrics = evaluate_query(spec, results)
    truth = truth_of(run, CAPTURE[mark:]) if run is not None else None
    return {
        "runner": "josty",
        "kind": kind,  # "ranking" (stable source) or "truth" (scraper group)
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
        "truth": truth,
    }


def _line(row: dict) -> str:
    truth = row.get("truth") or {"providers": [], "judged": 0, "mislabels": 0}
    engines = " ".join(
        f"{p['provider']}:{p['error_kind'] or 'rows'}({p['reported_http_status'] or '-'})"
        for p in truth["providers"]
    )
    return (
        f"  [{row['query_id']:>20} r{row['run_idx']} {row['wall_clock_seconds']:.1f}s] "
        f"n={row['result_count']:>2} {engines} | mislabel {truth['mislabels']}/{truth['judged']}"
    )


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


async def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    install_capture()
    print(
        f"[{RUN_ID}] status truth: {len(QUERIES)} queries x {REPEATS} ranking runs "
        f"on {', '.join(STABLE_SLOTS)}, plus one truth run each on "
        f"{', '.join(SCRAPER_SLOTS)}"
    )
    print(f"Output: {RUN_DIR}")

    jsonl_path = RUN_DIR / "results.jsonl"
    all_rows: list[dict] = []
    with jsonl_path.open("w", encoding="utf-8") as jsonl_f:
        for qi, spec in enumerate(QUERIES):
            stable = (STABLE_SLOTS[qi % len(STABLE_SLOTS)],)
            scraper = (SCRAPER_SLOTS[qi % len(SCRAPER_SLOTS)],)
            runs = [(repeat, stable, "ranking") for repeat in range(REPEATS)]
            runs.append((0, scraper, "truth"))
            for run_idx, backends, kind in runs:
                row = await benchmark_one(spec, run_idx, backends, kind)
                all_rows.append(row)
                jsonl_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                jsonl_f.flush()
                print(f"{kind:<7}{_line(row)}", flush=True)
                await asyncio.sleep(INTER_REPEAT_SLEEP_S)
            # Let upstream rate limits reset between queries.
            await asyncio.sleep(INTER_QUERY_SLEEP_S)

    summary = _aggregate(all_rows)
    with (RUN_DIR / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    csv_path = RUN_DIR / "per_query.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        if all_rows:
            writer = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)

    rate = summary["mislabel_rate"]
    print(
        f"\nMislabel rate: {'no judged engine (no data point)' if rate is None else rate} "
        f"({summary['mislabels']} of {summary['judged']} zero-row engines)"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "all_runs"}, indent=2))
    print(f"\nWrote {jsonl_path}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {RUN_DIR / 'summary.json'}")


def _aggregate(rows: list[dict]) -> dict:
    # Ranking numbers come from the stable sources only; truth from every row.
    ranking = [r for r in rows if r.get("kind", "ranking") == "ranking"]
    judged = sum((r.get("truth") or {}).get("judged", 0) for r in rows)
    mislabels = sum((r.get("truth") or {}).get("mislabels", 0) for r in rows)
    return {
        "queries": len({r["query_id"] for r in ranking}),
        "runs": len(ranking),
        "truth_runs": len(rows) - len(ranking),
        "empty_runs": sum(1 for r in ranking if r["result_count"] == 0),
        "error_runs": sum(1 for r in rows if r.get("error")),
        "judged": judged,
        "mislabels": mislabels,
        "mislabel_rate": round(mislabels / judged, 4) if judged else None,
        "all_runs": _stats_view(ranking),
    }


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
