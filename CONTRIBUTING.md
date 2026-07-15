# Contributing

1. Use Python 3.11 or newer.
2. Install development dependencies with `python -m pip install -e ".[dev]"`.
3. Keep the skill shell-first, keyless by default, and independent of MCP or an LLM.
4. Add tests for behavior changes and provider failure modes.
5. Run `pytest -q`, `ruff check .`, and `python -m build` before submitting a change.

Do not add CAPTCHA, authentication, paywall, robots, or provider-control bypasses. New providers must expose failures and document credentials, terms, rate limits, and licensing implications.
