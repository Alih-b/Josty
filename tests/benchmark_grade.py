"""Relevance grading.

Default mode is deterministic and uses three signals in order:

  - canonical URL match        -> grade 3
  - any `answer` string hit    -> grade 2
  - subject-token co-occurrence-> grade 1
  - explicit avoid-list hit    -> grade -1
  - otherwise                  -> grade 0

LLM-as-judge mode (optional):
  Set `BENCHMARK_LLM_JUDGE=1` and provide an LLM callable via the
  `--judge` CLI flag of `benchmark.py`. In LLM mode each (query, top-5
  result) pair is scored 0..3 by the model; the union is normalized to
  the same {0, 1, 2, 3} scale used by the string grader. The string
  grader still runs and is logged as `string_grade` for comparison.

Both graders are intentionally conservative: when in doubt, grade 0.
The report calls out the grader choice per run.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


def _canonical_key(url: str) -> str:
    """Normalize a URL the way `canonical()` does, for comparison."""
    from urllib.parse import urlsplit

    p = urlsplit(url.strip())
    return f"{p.scheme.lower()}://{(p.hostname or '').lower()}{p.path.rstrip('/') or '/'}"


def string_grade(
    query: str,
    canonical_urls: Sequence[str],
    answer_strings: Sequence[str],
    result: dict,
) -> int:
    """Return an integer relevance grade in {-1, 0, 1, 2, 3}.

    `result` must have keys: url, title, snippet.
    """
    url = (result.get("url") or "").strip()
    title = (result.get("title") or "")
    snippet = (result.get("snippet") or "")
    text = f"{title} {snippet}".lower()

    # canonical URL match (post-normalization)
    key = _canonical_key(url) if url else ""
    if key and key in {_canonical_key(u) for u in canonical_urls}:
        return 3

    # answer string hit
    for needle in answer_strings:
        if needle.lower() in text:
            return 2

    # weak subject co-occurrence
    subject_tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 3]
    threshold = max(2, len(subject_tokens) // 2)
    if subject_tokens and sum(1 for t in subject_tokens if t in text) >= threshold:
        return 1

    return 0


def grade_run(
    query_spec: dict,
    results: list[dict],
) -> list[int]:
    """Grade a full result list against one query spec."""
    return [
        string_grade(
            query_spec["query"],
            query_spec["canonical"],
            query_spec["answers"],
            r,
        )
        for r in results
    ]
