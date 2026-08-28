import importlib.util
from pathlib import Path

LAUNCHER_PATH = (
    Path(__file__).parents[1] / ".agents" / "skills" / "josty" / "scripts" / "run.py"
)
SPEC = importlib.util.spec_from_file_location("josty_launcher", LAUNCHER_PATH)
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


def test_bootstrap_with_uv_executes_commands(tmp_path, monkeypatch):
    calls = []

    def fake_which(cmd):
        return "/usr/bin/uv" if cmd == "uv" else None

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)

    environment = tmp_path / ".venv"
    executable = launcher.python_in(environment)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("ddgs\n", encoding="utf-8")

    success = launcher._bootstrap_with_uv(executable, environment, requirements)
    assert success is True
    assert len(calls) == 2
    assert calls[0] == ["/usr/bin/uv", "venv", str(environment)]
    assert calls[1] == [
        "/usr/bin/uv",
        "pip",
        "install",
        "--python",
        str(executable),
        "-r",
        str(requirements),
    ]


def test_bootstrap_with_uv_handles_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    environment = tmp_path / ".venv"
    executable = launcher.python_in(environment)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("ddgs\n", encoding="utf-8")

    success = launcher._bootstrap_with_uv(executable, environment, requirements)
    assert success is False


