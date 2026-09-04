"""Unit tests for the Josty CLI argument contract."""

import json

import pytest
from josty.cli import main, parser
from josty.engine import DiagnoseRun


def test_query_defaults_are_explicit():
    args = parser().parse_args(["term"])
    assert (args.query, args.diagnose) == ("term", False)
    diagnose_args = parser().parse_args(["--diagnose"])
    assert (diagnose_args.query, diagnose_args.diagnose) == ("", True)


def test_results_only_conflicts_with_diagnose(monkeypatch):
    monkeypatch.setattr("sys.argv", ["josty", "--diagnose", "--results-only", "query"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2


def test_main_prints_json_diagnosis(monkeypatch, capsys):
    captured = {}

    async def diagnose(self, include_github=False, category="text"):
        captured.update(include_github=include_github, category=category)
        return DiagnoseRun()

    monkeypatch.setattr("josty.engine.Josty.diagnose_run", diagnose)
    monkeypatch.setattr("sys.argv", ["josty", "--diagnose", "--github"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert captured == {"include_github": True, "category": "text"}


def test_main_without_query_or_diagnose_fails(monkeypatch):
    monkeypatch.setattr("sys.argv", ["josty"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2


def test_main_clear_cache(monkeypatch, capsys):
    cleared = False

    def fake_clear(self):
        nonlocal cleared
        cleared = True

    monkeypatch.setattr("josty.engine.Josty.clear_cache", fake_clear)
    monkeypatch.setattr("sys.argv", ["josty", "--clear-cache"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cleared"
    assert cleared is True


def test_main_cache_stats(monkeypatch, capsys):
    stats = {"rows": 3, "bytes": 1200, "hits": 7}

    def fake_cache_stats(self):
        return stats

    monkeypatch.setattr("josty.engine.Josty.cache_stats", fake_cache_stats)
    monkeypatch.setattr("sys.argv", ["josty", "--cache-stats"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload == stats


def test_parser_handles_cache_stats_flag():
    args = parser().parse_args(["--cache-stats"])
    assert args.cache_stats is True
    args_default = parser().parse_args(["query"])
    assert args_default.cache_stats is False


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


def test_cli_stdout_is_strictly_valid_json_even_with_stderr_warnings(monkeypatch, capsys):
    """Assert stdout contains valid JSON and nothing else, even when stderr has warnings."""
    import sys

    from josty.engine import SearchRun

    async def fake_research(self, *args, **kwargs):
        # Simulate stderr output like rustls native root cert warnings or third-party loggers
        print(
            "failed to load native root certificate: Permission denied",
            file=sys.stderr,
        )
        return SearchRun(query="test", results=[], providers=[])

    monkeypatch.setattr("josty.engine.Josty.research_run", fake_research)
    monkeypatch.setattr("sys.argv", ["josty", "test"])
    main()

    captured = capsys.readouterr()
    raw_stdout = captured.out
    raw_stderr = captured.err

    # Stderr has the warning
    assert "failed to load native root certificate" in raw_stderr

    # Stdout must be strictly valid JSON with zero non-JSON prefix or suffix
    assert raw_stdout.startswith("{") or raw_stdout.startswith("[")
    parsed = json.loads(raw_stdout)
    assert isinstance(parsed, dict)
    assert parsed["status"] == "complete"
    assert parsed["query"] == "test"


def test_cli_sanitizes_nan_instead_of_exiting(monkeypatch, capsys):
    from josty.engine import SearchResult, SearchRun

    async def fake_research(self, *args, **kwargs):
        return SearchRun(
            query="nan-test",
            results=[SearchResult("t", "https://example.com/x", score=float("nan"))],
        )

    monkeypatch.setattr("josty.engine.Josty.research_run", fake_research)
    monkeypatch.setattr("sys.argv", ["josty", "nan-test"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["score"] is None


def test_parser_handles_max_query_variants_flag():
    args_default = parser().parse_args(["query"])
    assert args_default.max_query_variants is None

    args = parser().parse_args(["query", "--max-query-variants", "3"])
    assert args.max_query_variants == 3

    with pytest.raises(SystemExit):
        parser().parse_args(["query", "--max-query-variants", "invalid"])


def test_parser_handles_max_content_chars_flag():
    args_default = parser().parse_args(["query"])
    assert args_default.max_content_chars == 8000

    args = parser().parse_args(["query", "--max-content-chars", "4000"])
    assert args.max_content_chars == 4000

    args_zero = parser().parse_args(["query", "--max-content-chars", "0"])
    assert args_zero.max_content_chars == 0

    with pytest.raises(SystemExit):
        parser().parse_args(["query", "--max-content-chars", "invalid"])


def test_version_flag_prints_single_source_version(capsys):
    from josty.engine import __version__

    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert f"josty {__version__}" in capsys.readouterr().out


def test_version_flag_ends_only_flags_run(monkeypatch, capsys):
    from josty.engine import __version__

    monkeypatch.setattr("sys.argv", ["josty", "--version"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert f"josty {__version__}" in capsys.readouterr().out
