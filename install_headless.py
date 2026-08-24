from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SKILL_SOURCE = REPO_ROOT / "skills" / "super-speech" / "SKILL.md"


def runtime_directory() -> Path:
    configured = os.environ.get("SUPER_SPEECH_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".super-speech"


def virtualenv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def engine_command(environment: Path) -> Path:
    name = "super-speech-engine.exe" if os.name == "nt" else "super-speech-engine"
    return environment / ("Scripts" if os.name == "nt" else "bin") / name


def install_agent_skills() -> list[Path]:
    installed: list[Path] = []
    for agent_directory in (".codex", ".claude"):
        agent_home = Path.home() / agent_directory
        if not agent_home.is_dir():
            continue
        target = agent_home / "skills" / "super-speech" / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL_SOURCE, target)
        installed.append(target)
    return installed


def main() -> None:
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        raise SystemExit("Headless Super Speech requires Python 3.11, 3.12, or 3.13")
    if sys.platform == "darwin":
        mac_version = platform.mac_ver()[0]
        mac_major = int(mac_version.split(".", 1)[0]) if mac_version else 0
        if platform.machine() != "arm64" or mac_major < 14:
            raise SystemExit(
                "Headless Super Speech currently requires Apple Silicon and macOS 14 or newer"
            )

    runtime = runtime_directory()
    environment = runtime / "engine"
    if not virtualenv_python(environment).is_file():
        venv.EnvBuilder(with_pip=True).create(environment)

    python = virtualenv_python(environment)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", str(REPO_ROOT)],
        check=True,
    )
    engine = engine_command(environment)
    subprocess.run([str(engine), "setup"], check=True)
    skills = install_agent_skills()

    print(f"Installed headless Super Speech at {engine}")
    for skill in skills:
        print(f"Installed skill at {skill}")


if __name__ == "__main__":
    main()
