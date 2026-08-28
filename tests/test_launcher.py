import importlib.util
from pathlib import Path

LAUNCHER_PATH = (
    Path(__file__).parents[1] / ".agents" / "skills" / "deep-search" / "scripts" / "run.py"
)
SPEC = importlib.util.spec_from_file_location("deep_search_launcher", LAUNCHER_PATH)
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


def test_environment_marker_tracks_requirements(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1\n", encoding="utf-8")
    environment = tmp_path / ".venv"
    environment.mkdir()
    executable = launcher.python_in(environment)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    marker = environment / ".requirements.sha256"

    digest = launcher.requirements_digest(requirements)
    assert not launcher.environment_ready(executable, marker, digest)
    marker.write_text(digest, encoding="utf-8")
    assert launcher.environment_ready(executable, marker, digest)

    requirements.write_text("example==2\n", encoding="utf-8")
    assert not launcher.environment_ready(
        executable, marker, launcher.requirements_digest(requirements)
    )


def test_setup_lock_is_released(tmp_path):
    lock = tmp_path / "setup.lock"
    with launcher.setup_lock(lock):
        assert lock.is_dir()
    assert not lock.exists()


def test_has_required_modules_and_fast_path(tmp_path, monkeypatch):
    assert launcher.has_required_modules(Path("/nonexistent/python")) is False

    # Simulate ready runtime without running network pip
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("ddgs\nhttpx\ntrafilatura\n", encoding="utf-8")
    environment = tmp_path / ".venv"
    executable = launcher.python_in(environment)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()

    monkeypatch.setattr(launcher, "has_required_modules", lambda exe: True)
    ready_exe = launcher.prepare_environment(environment, requirements)
    assert ready_exe == executable
    marker = environment / ".requirements.sha256"
    assert marker.is_file()

