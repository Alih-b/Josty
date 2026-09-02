"""Run the Josty CLI, installing it from PyPI on first use if needed.

This is a thin delegator for source-tree agents that do not have `josty` on PATH.
It never bundles engine code: the package is installed from PyPI and invoked as a
console script, so the single dependency manifest is `pyproject.toml`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def _ensure_josty() -> None:
    """Install josty from PyPI if the console script is not already available."""
    if shutil.which("josty") is not None:
        return
    uv = shutil.which("uv")
    if uv is not None:
        subprocess.run([uv, "tool", "install", "josty"], check=True)
        return
    pipx = shutil.which("pipx")
    if pipx is not None:
        subprocess.run([pipx, "install", "josty"], check=True)
        return
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "josty"], check=True
    )


def main() -> None:
    try:
        _ensure_josty()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Josty install failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(subprocess.call(["josty", *sys.argv[1:]]))


if __name__ == "__main__":
    main()
