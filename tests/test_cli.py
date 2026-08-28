"""Unit tests for the deep-search CLI argument contract."""

import json

import pytest
from deep_search.cli import main, parser
from deep_search.engine import DiagnoseRun


def test_query_defaults_are_explicit():
    args = parser().parse_args(["term"])
    assert (args.query, args.diagnose) == ("term", False)
    diagnose_args = parser().parse_args(["--diagnose"])
    assert (diagnose_args.query, diagnose_args.diagnose) == ("", True)


def test_results_only_conflicts_with_diagnose(monkeypatch):
    monkeypatch.setattr("sys.argv", ["deep-search", "--diagnose", "--results-only", "query"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2


def test_main_prints_json_diagnosis(monkeypatch, capsys):
    captured = {}

    async def diagnose(self, include_github=False, category="text"):
        captured.update(include_github=include_github, category=category)
        return DiagnoseRun()

    monkeypatch.setattr("deep_search.engine.DeepSearch.diagnose_run", diagnose)
    monkeypatch.setattr("sys.argv", ["deep-search", "--diagnose", "--github"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert captured == {"include_github": True, "category": "text"}


def test_main_without_query_or_diagnose_fails(monkeypatch):
    monkeypatch.setattr("sys.argv", ["deep-search"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2


def test_main_clear_cache(monkeypatch, capsys):
    cleared = False

    def fake_clear(self):
        nonlocal cleared
        cleared = True

    monkeypatch.setattr("deep_search.engine.DeepSearch.clear_cache", fake_clear)
    monkeypatch.setattr("sys.argv", ["deep-search", "--clear-cache"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cleared"
    assert cleared is True


def test_parser_handles_no_cache_flag():
    args = parser().parse_args(["query", "--no-cache"])
    assert args.no_cache is True
    args_default = parser().parse_args(["query"])
    assert args_default.no_cache is False


def test_parser_handles_profile_flag():
    args_default = parser().parse_args(["query"])
    assert args_default.profile == "general"

    args_dev = parser().parse_args(["query", "--profile", "dev"])
    assert args_dev.profile == "dev"

    args_academic = parser().parse_args(["query", "--profile", "academic"])
    assert args_academic.profile == "academic"

    with pytest.raises(SystemExit):
        parser().parse_args(["query", "--profile", "invalid"])
