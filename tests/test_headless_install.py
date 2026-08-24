from __future__ import annotations

import importlib.util
from pathlib import Path

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
