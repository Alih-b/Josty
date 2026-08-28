"""Run the bundled Deep Search CLI in a private environment."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import venv
from contextlib import contextmanager, suppress
from pathlib import Path

LOCK_WAIT_SECONDS = 180
LOCK_STALE_SECONDS = 600


def python_in(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def requirements_digest(requirements: Path) -> str:
    return hashlib.sha256(requirements.read_bytes()).hexdigest()


def environment_ready(executable: Path, marker: Path, digest: str) -> bool:
    return (
        executable.exists()
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == digest
    )


@contextmanager
def setup_lock(path: Path):
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
                if age > LOCK_STALE_SECONDS:
                    path.rmdir()
                    continue
            except (FileNotFoundError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for the Deep Search environment lock"
                ) from None
            time.sleep(0.1)
    try:
        yield
    finally:
        with suppress(OSError):
            path.rmdir()


def has_required_modules(executable: Path) -> bool:
    try:
        proc = subprocess.run(
            [str(executable), "-c", "import ddgs, httpx, trafilatura"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _bootstrap_with_uv(executable: Path, environment: Path, requirements: Path) -> bool:
    uv_bin = shutil.which("uv")
    if not uv_bin:
        return False
    try:
        if not executable.exists():
            subprocess.run(
                [uv_bin, "venv", str(environment)],
                check=True,
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
        subprocess.run(
            [
                uv_bin,
                "pip",
                "install",
                "--python",
                str(executable),
                "-r",
                str(requirements),
            ],
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Deep Search uv bootstrap failed, falling back to pip: {exc}", file=sys.stderr)
        return False


def prepare_environment(environment: Path, requirements: Path) -> Path:
    executable = python_in(environment)
    marker = environment / ".requirements.sha256"
    digest = requirements_digest(requirements)
    if environment_ready(executable, marker, digest):
        return executable

    # Fast path: if current runtime or parent venv already satisfies dependencies, use it
    if has_required_modules(executable):
        marker.write_text(digest, encoding="utf-8")
        return executable

    root_venv = environment.parent.parent.parent / ".venv"
    root_executable = python_in(root_venv)
    if root_executable.exists() and has_required_modules(root_executable):
        return root_executable

    current_python = Path(sys.executable)
    if has_required_modules(current_python):
        return current_python

    with setup_lock(environment.with_name(f"{environment.name}.lock")):
        if environment_ready(executable, marker, digest):
            return executable

        if _bootstrap_with_uv(executable, environment, requirements):
            marker.write_text(digest, encoding="utf-8")
            return executable

        if not executable.exists():
            venv.EnvBuilder(with_pip=True, clear=False).create(environment)
            executable = python_in(environment)

        pip_bin = (
            environment
            / ("Scripts" if os.name == "nt" else "bin")
            / ("pip.exe" if os.name == "nt" else "pip")
        )
        if not pip_bin.exists():
            subprocess.run(
                [str(executable), "-m", "ensurepip", "--upgrade"],
                check=True,
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
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
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        marker.write_text(digest, encoding="utf-8")
    return executable


def main() -> None:
    skill_dir = Path(__file__).resolve().parent.parent
    source = skill_dir / "src"
    requirements = skill_dir / "requirements.txt"
    if not (source / "deep_search" / "cli.py").is_file() or not requirements.is_file():
        print("Deep Search skill is incomplete; reinstall it.", file=sys.stderr)
        raise SystemExit(2)

    try:
        executable = prepare_environment(skill_dir / ".venv", requirements)
    except (OSError, subprocess.CalledProcessError, TimeoutError) as exc:
        print(f"Deep Search environment setup failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source), env.get("PYTHONPATH", "")) if part
    )
    command = [str(executable), "-m", "deep_search.cli", *sys.argv[1:]]
    raise SystemExit(subprocess.call(command, env=env))


if __name__ == "__main__":
    main()
