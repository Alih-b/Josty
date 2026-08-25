"""IR metrics, following TREC/BEIR conventions.

All metrics take a list of result rows, where each row is a dict:

    {"url": str, "title": str, "snippet": str}

Relevance is computed per (query, row) via `grade()` from
`benchmark_grade`.

Metrics implemented:
  dcg_at_k(rel, k)        — Discounted Cumulative Gain, log2 discount
  ndcg_at_k(rel, k)       — Normalized DCG; assumes binary ideal order
  mrr(rankings)           — Mean Reciprocal Rank of first relevant result
  precision_at_k(rel, k)  — #relevant / k
  graded_recall(rel, k)   — sum(rel)/sum(ideal_rel) within top-k

`bootstrap_ci(samples, fn, n=2000)` returns (point_estimate, low, high)
for the 95% percentile bootstrap CI.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence


def dcg_at_k(rels: Sequence[float], k: int) -> float:
    """DCG with the standard log2(rank+1) discount."""
    score = 0.0
    for i, rel in enumerate(rels[:k]):
        # +2 because log2(1)=0, log2(2)=1, so the first rank uses log2(2).
        score += rel / math.log2(i + 2)
    return score


def ndcg_at_k(rels: Sequence[float], k: int) -> float:
    """Normalized DCG@k assuming the ideal ordering sorts rels descending."""
    ideal = sorted(rels, reverse=True)
    denom = dcg_at_k(ideal, k)
    if denom == 0:
        return 0.0
    return dcg_at_k(rels, k) / denom


def mrr_one(rels: Sequence[float]) -> float:
    """Reciprocal rank of the first result with rel>0. 0 if none."""
    for i, rel in enumerate(rels, 1):
        if rel > 0:
            return 1.0 / i
    return 0.0


def precision_at_k(rels: Sequence[float], k: int) -> float:
    relevant = sum(1 for r in rels[:k] if r > 0)
    return relevant / max(k, 1)


def graded_recall_at_k(rels: Sequence[float], ideal_total: float, k: int) -> float:
    if ideal_total <= 0:
        return 0.0
    return sum(rels[:k]) / ideal_total


def bootstrap_ci(
    samples: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = lambda xs: sum(xs) / len(xs),
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Percentile bootstrap CI for `statistic` over `samples`.

    Returns (point_estimate, lower, upper).
    """
    if not samples:
        return 0.0, 0.0, 0.0
    point = statistic(samples)
    rng = random.Random(seed)
    n = len(samples)
    boots = []
    for _ in range(n_resamples):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        boots.append(statistic(resample))
    boots.sort()
    low_idx = int((alpha / 2) * n_resamples)
    high_idx = int((1 - alpha / 2) * n_resamples) - 1
    return point, boots[low_idx], boots[high_idx]


def percentile(samples: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile, no numpy."""
    if not samples:
        return 0.0
    s = sorted(samples)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def latency_stats(samples: Sequence[float]) -> dict:
    """Return mean, std, p50, p95. All seconds."""
    if not samples:
        return {"mean": 0.0, "std": 0.0, "p50": 0.0, "p95": 0.0}
    mean = sum(samples) / len(samples)
    var = sum((x - mean) ** 2 for x in samples) / max(len(samples) - 1, 1)
    return {
        "mean": round(mean, 3),
        "std": round(math.sqrt(var), 3),
        "p50": round(percentile(samples, 0.50), 3),
        "p95": round(percentile(samples, 0.95), 3),
    }


def paired_per_query(
    rows: list[dict], runner_a: str, runner_b: str
) -> dict[str, list[tuple[str, float, float]]]:
    """Pair rows by (query_id, run_idx) and return per-metric diff arrays.

    Returns a dict mapping metric name to a list of
    (query_id, value_a, value_b) tuples for every (query_id, run_idx)
    pair that exists in *both* runners' rows.
    """
    by_key: dict[tuple[str, int], dict[str, dict]] = {}
    for r in rows:
        key = (r["query_id"], r["run_idx"])
        runner = r["runner"]
        by_key.setdefault(key, {})[runner] = r

    paired: dict[str, list[tuple[str, float, float]]] = {}
    for key, runners in by_key.items():
        if runner_a not in runners or runner_b not in runners:
            continue
        a, b = runners[runner_a], runners[runner_b]
        for metric in ("ndcg_at_10", "mrr", "wall_clock_seconds"):
            paired.setdefault(metric, []).append(
                (key[0], float(a[metric]), float(b[metric]))
            )
    return paired


def wilcoxon_signed_rank(
    diffs: Sequence[float],
) -> dict:
    """Wilcoxon signed-rank test, two-sided, scipy-free.

    Returns {"W_plus", "W_minus", "W", "n_nonzero", "p_value_approx"}.
    The p-value is a normal-approximation good enough for n_nonzero >= 10;
    for smaller n the result reports `p_value_approx=None` and you should
    use an exact-table or permutation test.

    For the benchmark we report the statistic and the approximation, and
    call out when n is too small for the normal approximation.
    """
    nonzero = [(abs(d), 1 if d > 0 else -1) for d in diffs if d != 0]
    if not nonzero:
        return {
            "n": 0,
            "n_nonzero": 0,
            "W_plus": 0,
            "W_minus": 0,
            "W": 0,
            "p_value_approx": None,
            "note": "no non-zero differences",
        }
    nonzero.sort(key=lambda x: x[0])
    n = len(nonzero)
    # Average ranks for ties.
    ranks: list[float] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and nonzero[j + 1][0] == nonzero[i][0]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for _k in range(i, j + 1):
            ranks.append(avg_rank)
        i = j + 1
    signs = [s for _, s in nonzero]
    w_plus = sum(r for r, s in zip(ranks, signs, strict=True) if s > 0)
    w_minus = sum(r for r, s in zip(ranks, signs, strict=True) if s < 0)
    w = min(w_plus, w_minus)

    p_value = None
    note = ""
    if n >= 10:
        # Normal approximation with continuity correction.
        mean_w = n * (n + 1) / 4.0
        var_w = n * (n + 1) * (2 * n + 1) / 24.0
        if var_w > 0:
            z = (w - mean_w) / math.sqrt(var_w)
            # Two-sided p-value from standard normal, no scipy.
            p_value = _two_sided_normal_p(abs(z))
            note = "normal approximation, n>=10"
    else:
        note = "n too small for normal approximation; treat p as 'large'"

    return {
        "n": len(diffs),
        "n_nonzero": n,
        "W_plus": round(w_plus, 4),
        "W_minus": round(w_minus, 4),
        "W": round(w, 4),
        "p_value_approx": None if p_value is None else round(p_value, 4),
        "note": note,
    }


def _two_sided_normal_p(z: float) -> float:
    """Two-sided p-value for standard normal at |z|.

    Uses a high-accuracy rational approximation of the standard normal
    CDF (Hart/Cody-style), accurate to ~1e-9 across the real line. The
    one-sided Phi(z) is computed, then we double the upper tail.
    """
    # Phi(z) for z >= 0; reflect for z < 0.
    if z < 0:
        return 2.0 * _phi(z)
    tail = _phi(-z)  # upper tail = Phi(-z) for z >= 0
    return min(max(2.0 * tail, 0.0), 1.0)


def _phi(z: float) -> float:
    """Standard normal CDF, accurate to ~1e-9.

    Algorithm: West's algorithm (2009), a few high-order rational
    coefficients. Not the fastest, but unambiguous and good enough for
    reporting p-values to 4 decimal places.
    """
    if z < -8.0:
        return 0.0
    if z > 8.0:
        return 1.0
    # Abramowitz & Stegun 7.1.26, |error| < 1.5e-7.
    t = 1.0 / (1.0 + 0.2316419 * abs(z))
    d = 0.3989422804014327  # 1 / sqrt(2*pi)
    p = (
        d
        * math.exp(-0.5 * z * z)
        * (
            t
            * (
                0.319381530
                + t
                * (
                    -0.356563782
                    + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))
                )
            )
        )
    )
    return 1.0 - p if z >= 0 else p
