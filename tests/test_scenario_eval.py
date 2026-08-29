"""Offline pytest wrapper for the scenario eval harness."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_eval import (
    DEFAULT_CORPUS,
    DEFAULT_LIVE_OUT,
    DEFAULT_OUT,
    evaluate_corpus,
    evaluate_payload,
    live_output_dir,
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


def test_corpus_has_no_extra_rows(corpus):
    extra = sorted(set(corpus) - {spec["id"] for spec in SCENARIOS})
    assert extra == []


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
    assert "Skill: require subject tokens before citing" in report


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


def test_forbid_token_without_must_answer():
    spec = {
        "id": "forbid_only",
        "layer": "news",
        "query": "q",
        "flags": {},
        "min_results": 1,
        "expect_status": "complete",
        "forbid_if_missing_must": ["3.15"],
        "label_if_fail": "upstream_quality",
        "pathway": "test",
    }
    payload = {
        "schema_version": "1.0",
        "query": "q",
        "status": "complete",
        "count": 1,
        "partial": False,
        "providers": [],
        "results": [{"title": "Python 3.15 notes", "url": "https://example.com/a", "snippet": ""}],
    }
    row = evaluate_payload(spec, payload)
    assert row.verdict == "fail"
    assert any("forbidden token" in issue for issue in row.issues)


def test_diagnose_4xx_must_remain_ok():
    spec = scenario_by_id("diagnose_reachability")
    payload = {
        "schema_version": "1.0",
        "status": "complete",
        "reachable": 1,
        "count": 1,
        "providers": [
            {
                "provider": "brave",
                "host": "search.brave.com",
                "ok": False,
                "http_status": 429,
                "error_kind": None,
                "error": None,
            }
        ],
    }
    row = evaluate_payload(spec, payload)
    assert row.verdict == "fail"
    assert row.taxonomy_class == "intended_misleading"
    assert any("429" in issue for issue in row.issues)


def test_missing_corpus_row_is_contract_bug():
    results = evaluate_corpus({})
    by_id = {row.id: row for row in results}
    assert by_id["academic_profile_rag"].taxonomy_class == "contract_bug"
    assert by_id["academic_profile_rag"].issues == ["missing corpus row"]


def test_live_output_dir_never_uses_replay(tmp_path: Path):
    assert live_output_dir(DEFAULT_OUT) == DEFAULT_LIVE_OUT
    assert live_output_dir(DEFAULT_OUT / "nested") == DEFAULT_LIVE_OUT
    custom = tmp_path / "custom-live"
    assert live_output_dir(custom) == custom.resolve()


def test_live_main_does_not_write_replay(monkeypatch, tmp_path: Path):
    from scenario_eval import main

    written: dict[str, Path] = {}

    def fake_capture(out_dir: Path) -> Path:
        written["capture"] = out_dir
        dest = out_dir / "scenario_corpus.live.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("", encoding="utf-8")
        return dest

    def fake_write(out_dir: Path, results, *, source="frozen"):
        written["write"] = out_dir
        written["source"] = source

    monkeypatch.setenv("JOSTY_LIVE_EVAL", "1")
    monkeypatch.setattr("scenario_eval.capture_live", fake_capture)
    monkeypatch.setattr("scenario_eval.evaluate_corpus", lambda corpus: [])
    monkeypatch.setattr("scenario_eval.write_outputs", fake_write)
    assert main(["--live", "--out", str(DEFAULT_OUT)]) == 0
    assert written["capture"] == DEFAULT_LIVE_OUT
    assert written["write"] == DEFAULT_LIVE_OUT
    assert written["source"] == "live"
    assert not (DEFAULT_OUT / "clobbered").exists()
