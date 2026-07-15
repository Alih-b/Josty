"""Portable launcher for the Deep Search skill.

The canonical Python source ships inside the skill. If CLI dependencies are missing,
this launcher creates a private virtual environment and installs them once. No API key,
MCP server, global package installation, or repository checkout is required.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import venv
from pathlib import Path


def python_in(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def has_cli_dependencies() -> bool:
    dependencies = ("ddgs", "httpx", "trafilatura")
    return all(importlib.util.find_spec(name) is not None for name in dependencies)


def launch(executable: Path, source: Path) -> int:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source) if not existing else os.pathsep.join((str(source), existing))
    )
    command = [str(executable), "-m", "deep_search.cli", *sys.argv[1:]]
    return subprocess.call(command, env=environment)


def main() -> None:
    skill_dir = Path(__file__).resolve().parent.parent
    source = skill_dir / "src"
    requirements = skill_dir / "requirements.txt"
    if not (source / "deep_search" / "cli.py").is_file():
        print("Deep Search skill source is incomplete; reinstall the skill.", file=sys.stderr)
        raise SystemExit(2)

    if has_cli_dependencies():
        raise SystemExit(launch(Path(sys.executable), source))

    environment = skill_dir / ".venv"
    executable = python_in(environment)
    if not executable.exists():
        venv.EnvBuilder(with_pip=True, clear=False).create(environment)
        subprocess.run(
            [
                str(executable),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
            check=True,
        )
    raise SystemExit(launch(executable, source))


if __name__ == "__main__":
    main()
