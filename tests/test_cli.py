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
    async def diagnose(self):
        return DiagnoseRun()

    monkeypatch.setattr("deep_search.engine.DeepSearch.diagnose_run", diagnose)
    monkeypatch.setattr("sys.argv", ["deep-search", "--diagnose"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"


def test_main_without_query_or_diagnose_fails(monkeypatch):
    monkeypatch.setattr("sys.argv", ["deep-search"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2