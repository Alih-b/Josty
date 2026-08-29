"""Offline pytest wrapper for the scenario eval harness."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_eval import (
    DEFAULT_CORPUS,
    evaluate_corpus,
    evaluate_payload,
    load_corpus,
    render_report,
)
from scenario_queries import SCENARIOS, scenario_by_id

EXPECTED_VERDICTS = {
    "news_token_collision": ("fail", "upstream_quality"),
    "news_near_miss": ("fail", "upstream_quality"),
    "academic_profile_rag": ("fail", "product_gap"),
    "dev_profile_fastapi": ("pass", None),
    "site_filter_httpx": ("pass", None),
    "exact_free_threading": ("pass", None),
    "fetch_rrf": ("pass", None),
    "diagnose_reachability": ("pass", None),
    "linux_kernel_year": ("pass", None),
    "empty_provider_complete": ("pass", None),
}


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(DEFAULT_CORPUS)


@pytest.fixture(scope="module")
def results(corpus):
    return evaluate_corpus(corpus)


def test_corpus_covers_every_spec(corpus):
    missing = [spec["id"] for spec in SCENARIOS if spec["id"] not in corpus]
    assert missing == []


def test_known_outcomes_match_frozen_corpus(results):
    by_id = {row.id: row for row in results}
    assert set(by_id) == set(EXPECTED_VERDICTS)
    for case_id, (verdict, klass) in EXPECTED_VERDICTS.items():
        row = by_id[case_id]
        assert row.verdict == verdict, f"{case_id}: {row.issues}"
        assert row.taxonomy_class == klass


def test_report_lists_taxonomy_and_pathway(results):
    report = render_report(results)
    assert "`news_token_collision`" in report
    assert "`upstream_quality`" in report
    assert "Lexical relevance gate" in report


def test_site_leak_is_contract_bug():
    spec = scenario_by_id("site_filter_httpx")
    payload = {
        "schema_version": "1.0",
        "query": spec["query"],
        "status": "complete",
        "count": 1,
        "partial": False,
        "providers": [{"provider": "test", "ok": True, "result_count": 1}],
        "results": [
            {
                "title": "leak",
                "url": "https://example.com/httpx",
                "snippet": "connection reset",
            }
        ],
    }
    row = evaluate_payload(spec, payload)
    assert row.verdict == "fail"
    assert row.taxonomy_class == "contract_bug"
    assert any("site leak" in issue for issue in row.issues)


def test_empty_corpus_has_no_rows(tmp_path: Path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert load_corpus(empty) == {}


def test_live_refuses_without_env(monkeypatch, tmp_path: Path):
    from scenario_eval import main

    monkeypatch.delenv("JOSTY_LIVE_EVAL", raising=False)
    assert main(["--live", "--out", str(tmp_path)]) == 2
