# Contributing

1. Use Python 3.11 or newer.
2. Install development dependencies with `python -m pip install -e ".[dev]"`.
3. Keep the skill shell-first, keyless by default, and independent of MCP or an LLM.
4. Add tests for behavior changes and provider failure modes.
5. Run `pytest -q`, `ruff check .`, and `python -m build` before submitting a change.
6. Run `python tests/scenario_eval.py` after changing search, rank, news, fetch, or diagnose behavior.

## How to label a live miss

Walk the decision tree in [docs/ISSUE_TAXONOMY.md](docs/ISSUE_TAXONOMY.md) and pick exactly one class (`contract_bug`, `intended_misleading`, `upstream_quality`, `product_gap`). Then add a spec to `tests/scenario_queries.py` and a frozen envelope to `tests/scenario_corpus.jsonl`. Do not change `engine.py` in the same change unless the class is `contract_bug`.

Optional live recapture (not CI):

```bash
JOSTY_LIVE_EVAL=1 python tests/scenario_eval.py --live --out tests/scenario_out/live
```

Do not add CAPTCHA, authentication, paywall, robots, or provider-control bypasses. New providers must expose failures and document credentials, terms, rate limits, and licensing implications.
