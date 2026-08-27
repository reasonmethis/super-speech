from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


INSTALLER = (
    Path(__file__).parents[1]
    / "skills"
    / "super-speech"
    / "scripts"
    / "install.py"
)


def load_installer():
    spec = importlib.util.spec_from_file_location("super_speech_installer", INSTALLER)
    assert spec and spec.loader
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer


def test_codex_destination_honors_codex_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = load_installer()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    assert installer.default_skill_directory("codex") == (
        tmp_path / "codex-home" / "skills" / "super-speech"
    )


def test_engine_environment_is_skill_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = load_installer()
    runtime = tmp_path / "skill" / "runtime"
    monkeypatch.setenv("SUPER_SPEECH_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("SUPER_SPEECH_MODEL_DIR", str(tmp_path / "other-models"))

    environment = installer.engine_environment(runtime)

    assert environment["SUPER_SPEECH_HOME"] == str(runtime)
    assert environment["SUPER_SPEECH_MODEL_DIR"] == str(runtime / "models" / "kokoro")


def test_copy_skill_excludes_generated_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = load_installer()
    source = tmp_path / "source" / "super-speech"
    (source / "engine").mkdir(parents=True)
    (source / "runtime").mkdir()
    (source / "engine" / "build").mkdir()
    (source / "SKILL.md").write_text("skill", encoding="utf-8")
    (source / "engine" / "engine.py").write_text("engine", encoding="utf-8")
    (source / "engine" / "build" / "wheel.py").write_text("build", encoding="utf-8")
    (source / "runtime" / "state.json").write_text("state", encoding="utf-8")
    monkeypatch.setattr(installer, "SOURCE_SKILL", source)

    destination = tmp_path / "installed" / "super-speech"
    installer.copy_skill(destination)

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert (destination / "engine" / "engine.py").is_file()
    assert not (destination / "engine" / "build").exists()
    assert not (destination / "runtime").exists()


def test_stop_existing_engine_retries_during_status_before_heartbeat_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = load_installer()
    engine = tmp_path / "venv" / "super-speech-engine"
    engine.parent.mkdir()
    engine.touch()
    runtime = tmp_path / "runtime"
    activity = iter((True, True, False))
    attempts: list[list[str]] = []

    monkeypatch.setattr(
        installer,
        "runtime_engine_is_active",
        lambda _runtime: next(activity),
    )

    def interrupt(command, **_kwargs):
        attempts.append(command)
        return SimpleNamespace(returncode=1 if len(attempts) == 1 else 0)

    monkeypatch.setattr(installer.subprocess, "run", interrupt)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)

    installer.stop_existing_engine(engine, runtime)

    assert attempts == [[str(engine), "interrupt"], [str(engine), "interrupt"]]


def test_status_detects_a_starting_engine_before_its_first_heartbeat(
    tmp_path: Path,
) -> None:
    installer = load_installer()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "status.json").write_text(
        json.dumps({"state": "loading", "engine_pid": os.getpid()}),
        encoding="utf-8",
    )

    assert installer.runtime_engine_is_active(runtime)


def test_install_stops_the_engine_before_updating_its_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installer = load_installer()
    destination = tmp_path / "skill"
    environment = destination / "runtime" / "venv"
    python = installer.virtualenv_python(environment)
    engine = installer.engine_command(environment)
    python.parent.mkdir(parents=True)
    python.touch()
    engine.touch()
    calls: list[str] = []

    monkeypatch.setattr(installer, "copy_skill", lambda _destination: None)
    monkeypatch.setattr(
        installer,
        "stop_existing_engine",
        lambda *_args: calls.append("stop"),
    )

    def run(command, **_kwargs):
        calls.append("pip" if "pip" in command else "setup")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer.subprocess, "run", run)

    installer.install(destination)

    assert calls == ["stop", "pip", "setup"]
