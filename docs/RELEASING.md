# Release checklist

1. Update `CHANGELOG.md` and the version in `pyproject.toml` and `deep_search/api.py`.
2. Confirm repository ownership, author metadata, and `[project.urls]` in `pyproject.toml`.
3. Enable GitHub private vulnerability reporting so `SECURITY.md` has a working contact path.
4. Review dependency licenses and upstream provider terms.
5. Run:

   ```bash
   python -m pip install -e ".[dev]"
   pytest -q
   ruff check .
   python -m build
   ```

6. Test the built wheel in a clean environment and run one text search, one news search, and one bounded fetch.
7. Verify the repository contains no credentials, private paths, generated environments, caches, or build artifacts.
8. Tag the release and publish the GitHub release before uploading the same artifacts to PyPI.
9. Never claim unlimited availability: DDGS and upstream engines may throttle, block, or change behavior.
