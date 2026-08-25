"""Offline replay scorer for the canonical benchmark.

Reads the frozen corpus (default: tests/benchmark_corpus.jsonl)
and writes:

  <out>/per_query.csv
  <out>/per_runner.json
  <out>/significance.json
  <out>/REPORT.md

The report adds paired Wilcoxon signed-rank tests vs `deep_search` for
nDCG@10, MRR, and wall-clock latency. Bootstrap 95% CIs on mean
differences are included alongside.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from benchmark_metrics import (  # noqa: E402
    bootstrap_ci,
    percentile,
    wilcoxon_signed_rank,
)

DEFAULT_CORPUS = HERE / "benchmark_corpus.jsonl"
DEFAULT_OUT = HERE / "benchmark_out" / "replay"


def load_corpus(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def per_runner_stats(rows: list[dict]) -> dict[str, dict]:
    by_runner: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_runner[r["runner"]].append(r)
    out: dict[str, dict] = {}
    for runner, rrows in by_runner.items():
        successful = [r for r in rrows if r["result_count"] > 0]
        out[runner] = {
            "queries": len({r["query_id"] for r in rrows}),
            "runs": len(rrows),
            "empty_runs": sum(1 for r in rrows if r["result_count"] == 0),
            "error_runs": sum(1 for r in rrows if r.get("error")),
            "all_runs": _describe_metrics(rrows),
            "successful_runs_only": _describe_metrics(successful),
        }
    return out


def _describe_metrics(rrows: list[dict]) -> dict:
    keys = ["ndcg_at_10", "mrr", "precision_at_5", "graded_recall_at_10"]
    out: dict[str, dict] = {}
    for k in keys:
        v = [float(r.get(k, 0.0)) for r in rrows]
        out[k] = _describe(v)
    lat = [float(r["wall_clock_seconds"]) for r in rrows]
    out["wall_clock_seconds"] = _describe(lat)
    counts = [int(r["result_count"]) for r in rrows]
    out["result_count"] = _describe(counts)
    uniques = [int(r["unique_post_canonical"]) for r in rrows]
    out["unique_post_canonical"] = _describe(uniques)
    return out


def _describe(values: list[float]) -> dict:
    if not values:
        return {"mean": 0.0, "std": 0.0, "median": 0.0, "p50": 0.0, "p95": 0.0, "n": 0}
    mean = statistics.fmean(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    median = statistics.median(values)
    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "median": round(median, 4),
        "p50": round(percentile(values, 0.50), 4),
        "p95": round(percentile(values, 0.95), 4),
        "n": len(values),
    }


def paired_tests(rows: list[dict], base: str) -> dict:
    """Paired Wilcoxon tests vs `base` runner for nDCG@10, MRR, latency.

    Returns `{runner: {"all_paired": {metric: ...}, "successful_paired": {metric: ...}}}`.
    The successful view filters pairs where both runners returned >0
    results; the all-paired view includes the empty runs (zeros).
    """
    # Build per-(query_id, run_idx) views per metric, plus a result-count map.
    by_key_metric: dict[str, dict[tuple[str, int], dict[str, float]]] = {
        m: {} for m in ("ndcg_at_10", "mrr", "wall_clock_seconds")
    }
    success_keys_metric: dict[str, set[tuple[str, int]]] = {
        m: set() for m in ("ndcg_at_10", "mrr", "wall_clock_seconds")
    }
    result_counts: dict[tuple[str, int], dict[str, int]] = {}
    for r in rows:
        key = (r["query_id"], r["run_idx"])
        for m in by_key_metric:
            by_key_metric[m].setdefault(key, {})[r["runner"]] = float(r[m])
        result_counts.setdefault(key, {})[r["runner"]] = r["result_count"]

    out: dict[str, dict] = {}
    for runner in sorted({r["runner"] for r in rows}):
        if runner == base:
            continue
        # Compute success keys (both runners returned >0) per metric.
        for m in by_key_metric:
            for key, vals in by_key_metric[m].items():
                if (
                    base in vals
                    and runner in vals
                    and result_counts[key].get(base, 0) > 0
                    and result_counts[key].get(runner, 0) > 0
                ):
                    success_keys_metric[m].add(key)

        out[runner] = {"all_paired": {}, "successful_paired": {}}
        for view_name, key_filter in (
            ("all_paired", None),
            ("successful_paired", "use success_keys_metric"),
        ):
            for m in ("ndcg_at_10", "mrr", "wall_clock_seconds"):
                if key_filter is None:
                    diffs = [
                        vals[base] - vals[runner]
                        for vals in by_key_metric[m].values()
                        if base in vals and runner in vals
                    ]
                else:
                    diffs = [
                        vals[base] - vals[runner]
                        for key, vals in by_key_metric[m].items()
                        if key in success_keys_metric[m]
                        and base in vals
                        and runner in vals
                    ]
                if not diffs:
                    out[runner][view_name][m] = {"n_pairs": 0, "note": "no pairs"}
                    continue
                test = wilcoxon_signed_rank(diffs)
                point, lo, hi = bootstrap_ci(
                    diffs,
                    statistic=lambda xs: sum(xs) / len(xs),
                )
                out[runner][view_name][m] = {
                    "n_pairs": len(diffs),
                    "mean_diff": round(point, 4),
                    "ci95_low": round(lo, 4),
                    "ci95_high": round(hi, 4),
                    "wilcoxon_W": test["W"],
                    "wilcoxon_W_plus": test["W_plus"],
                    "wilcoxon_W_minus": test["W_minus"],
                    "wilcoxon_n_nonzero": test["n_nonzero"],
                    "p_value_approx": test["p_value_approx"],
                    "note": test["note"],
                }
    return out


def render_report(
    corpus_path: Path,
    stats: dict[str, dict],
    tests: dict[str, dict],
    coverage: dict,
) -> str:
    a = []
    a.append("# Deep Search benchmark — frozen-corpus replay\n")
    a.append(f"**Corpus:** `{corpus_path.relative_to(HERE.parent)}`\n")
    a.append(
        "**Grader:** string predicates from "
        "`benchmark_grade.string_grade` (canonical URL = 3, "
        "answer string = 2, weak subject = 1, else 0).\n"
    )
    a.append(f"**Runners:** {', '.join(stats.keys())}\n")
    a.append(
        f"**Total scored (runner × query × repeat) tuples:** "
        f"{coverage['total']}\n"
    )
    a.append(
        "**Backend rotation:** each query slot uses one of three "
        "predefined backend groups cycled by index; `slot_backends` is "
        "logged per row in the JSONL.\n"
    )
    a.append(
        "**Statistical test:** paired Wilcoxon signed-rank, two-sided, "
        "deep_search as baseline. p-values are a normal approximation "
        "when n_nonzero ≥ 10; otherwise the test reports no p-value.\n"
    )
    a.append("")

    a.append("## Coverage matrix\n")
    a.append("| runner | distinct queries | runs | empty | errors |")
    a.append("|---|---:|---:|---:|---:|")
    for runner, s in stats.items():
        a.append(
            f"| `{runner}` | {s['queries']} | {s['runs']} "
            f"| {s['empty_runs']} | {s['error_runs']} |"
        )
    a.append("")

    def _row(runner_name: str, view_key: str, metric: str) -> str:
        v = stats[runner_name][view_key][metric]
        return f"{v['mean']:.3f} ± {v['std']:.3f} (n={v['n']})"

    a.append("## Aggregate IR metrics — ALL runs (mean ± std)\n")
    a.append(
        "Empty runs (engine returned 0 results, usually throttled) "
        "count as zeros.\n"
    )
    a.append(
        "| runner | nDCG@10 | MRR | P@5 | graded_recall@10 | "
        "latency mean (s) |"
    )
    a.append("|---|---|---|---|---|---|")
    for runner in stats:
        a.append(
            f"| `{runner}` "
            f"| {_row(runner, 'all_runs', 'ndcg_at_10')} "
            f"| {_row(runner, 'all_runs', 'mrr')} "
            f"| {_row(runner, 'all_runs', 'precision_at_5')} "
            f"| {_row(runner, 'all_runs', 'graded_recall_at_10')} "
            f"| {_row(runner, 'all_runs', 'wall_clock_seconds')} |"
        )
    a.append("")

    a.append("## Aggregate IR metrics — SUCCESSFUL runs only\n")
    a.append(
        "Filters out empty runs so engine quality is judged separately "
        "from reliability.\n"
    )
    a.append(
        "| runner | nDCG@10 | MRR | P@5 | graded_recall@10 | "
        "latency mean (s) |"
    )
    a.append("|---|---|---|---|---|---|")
    for runner in stats:
        a.append(
            f"| `{runner}` "
            f"| {_row(runner, 'successful_runs_only', 'ndcg_at_10')} "
            f"| {_row(runner, 'successful_runs_only', 'mrr')} "
            f"| {_row(runner, 'successful_runs_only', 'precision_at_5')} "
            f"| {_row(runner, 'successful_runs_only', 'graded_recall_at_10')} "
            f"| {_row(runner, 'successful_runs_only', 'wall_clock_seconds')} |"
        )
    a.append("")

    a.append("## Paired Wilcoxon signed-rank tests vs `deep_search`\n")
    a.append(
        "diffs = deep_search − other. Positive ΔnDCG@10 / ΔMRR means "
        "deep_search is *better*; positive Δlatency means deep_search is "
        "*slower*. Two views: `all_paired` includes empty runs (zero "
        "scores); `successful_paired` keeps only pairs where both "
        "runners returned >0 results.\n"
    )

    def _test_table(view_key: str) -> None:
        a.append(f"### View: `{view_key}`\n")
        a.append(
            "| runner | metric | mean diff | 95% CI | n_pairs | W | "
            "p (approx) | note |"
        )
        a.append("|---|---|---:|---|---:|---:|---:|---|")
        for runner, views in tests.items():
            for metric, t in views[view_key].items():
                if t.get("n_pairs", 0) == 0:
                    a.append(
                        f"| `{runner}` | `{metric}` | — | — | 0 "
                        f"| — | — | no pairs |"
                    )
                    continue
                ci = f"[{t['ci95_low']:+.3f}, {t['ci95_high']:+.3f}]"
                p = (
                    f"{t['p_value_approx']:.4f}"
                    if t["p_value_approx"] is not None
                    else "n/a"
                )
                a.append(
                    f"| `{runner}` | `{metric}` | {t['mean_diff']:+.4f} "
                    f"| {ci} | {t['n_pairs']} | {t['wilcoxon_W']} "
                    f"| {p} | {t['note']} |"
                )
        a.append("")

    _test_table("successful_paired")
    _test_table("all_paired")

    a.append("## Honest caveats\n")
    a.append(
        "- **Frozen corpus, not live runs.** This report scores responses "
        "captured against the full 20-query set with 8 repeats "
        "each; any throttle-induced gap was logged with `error` or "
        "`result_count == 0`, never silently filled.\n"
    )
    a.append(
        "- **Engine rotation by query slot.** Each query uses one of "
        "`bing,brave,duckduckgo`, `google,mojeek,startpage`, or "
        "`yandex,yahoo`; the slot index is logged as `slot_backends` "
        "per row. This is the dominant confounder when comparing runs "
        "with uneven engine exposure.\n"
    )
    a.append(
        "- **String-grader bias.** Grading is conservative substring "
        "matching; bias affects all runners equally, so deltas are "
        "the durable signal.\n"
    )
    a.append(
        "- **Wilcoxon normal approximation** is reported for "
        "n_nonzero ≥ 10. For very small n the p-value is null and "
        "the table says so.\n"
    )
    a.append(
        "- **One network, one run window.** Engine state was identical "
        "across runners for each query within a slot; absolute numbers "
        "will shift on a different day but the rank order is the "
        "durable signal.\n"
    )
    return "\n".join(a) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Score the frozen benchmark corpus.")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    rows = load_corpus(args.corpus)
    if not rows:
        raise SystemExit(f"corpus is empty: {args.corpus}")

    stats = per_runner_stats(rows)

    coverage_by_runner: dict[str, dict] = {}
    by_runner: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_runner[r["runner"]].append(r)
    for runner, rrows in by_runner.items():
        coverage_by_runner[runner] = {
            "rows": len(rrows),
            "distinct_queries": len({r["query_id"] for r in rrows}),
            "empty_runs": sum(1 for r in rrows if r["result_count"] == 0),
            "error_runs": sum(1 for r in rrows if r.get("error")),
        }
    coverage = {"total": len(rows), "by_runner": coverage_by_runner}

    tests = paired_tests(rows, base="deep_search")

    with (args.out / "per_runner.json").open("w", encoding="utf-8") as f:
        json.dump(
            {"stats": stats, "coverage": coverage, "tests_vs_deep_search": tests},
            f,
            indent=2,
        )
    with (args.out / "significance.json").open("w", encoding="utf-8") as f:
        json.dump(tests, f, indent=2)

    report = render_report(args.corpus, stats, tests, coverage)
    with (args.out / "REPORT.md").open("w", encoding="utf-8") as f:
        f.write(report)

    print(f"Wrote {args.out / 'per_runner.json'}")
    print(f"Wrote {args.out / 'significance.json'}")
    print(f"Wrote {args.out / 'REPORT.md'}")


if __name__ == "__main__":
    main()
