from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path


SOURCE_SKILL = Path(__file__).resolve().parents[1]


def default_skill_directory(agent: str) -> Path:
    if agent == "codex":
        agent_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    else:
        agent_home = Path.home() / ".claude"
    return agent_home.expanduser() / "skills" / "super-speech"


def virtualenv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def engine_command(environment: Path) -> Path:
    name = "super-speech-engine.exe" if os.name == "nt" else "super-speech-engine"
    return environment / ("Scripts" if os.name == "nt" else "bin") / name


def engine_environment(runtime: Path) -> dict[str, str]:
    return {
        **os.environ,
        "SUPER_SPEECH_HOME": str(runtime),
        "SUPER_SPEECH_MODEL_DIR": str(runtime / "models" / "kokoro"),
    }


def stop_existing_engine(engine: Path, runtime: Path) -> None:
    if not engine.is_file():
        return
    result = subprocess.run(
        [str(engine), "interrupt"],
        env=engine_environment(runtime),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return

    heartbeat = runtime / "engine.alive"
    deadline = time.monotonic() + 10
    while heartbeat.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if heartbeat.exists():
        raise RuntimeError("the existing headless engine did not stop before upgrade")


def validate_platform() -> None:
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        raise SystemExit("Headless Super Speech requires Python 3.11, 3.12, or 3.13")
    if sys.platform != "darwin":
        return

    mac_version = platform.mac_ver()[0]
    mac_major = int(mac_version.split(".", 1)[0]) if mac_version else 0
    if platform.machine() != "arm64" or mac_major < 14:
        raise SystemExit(
            "Headless Super Speech currently requires Apple Silicon and macOS 14 or newer"
        )


def copy_skill(destination: Path) -> None:
    source = SOURCE_SKILL.resolve()
    destination = destination.resolve()
    if destination == source:
        return
    if source in destination.parents or destination in source.parents:
        raise SystemExit("The destination must not contain the source skill, or vice versa")

    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "runtime", "build", "__pycache__", "*.egg-info", "*.pyc"
        ),
    )


def install(destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    copy_skill(destination)

    runtime = destination / "runtime"
    environment = runtime / "venv"
    stop_existing_engine(engine_command(environment), runtime)
    if not virtualenv_python(environment).is_file():
        venv.EnvBuilder(with_pip=True).create(environment)

    python = virtualenv_python(environment)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--upgrade",
            str(destination / "engine"),
        ],
        check=True,
    )
    engine = engine_command(environment)
    subprocess.run(
        [str(engine), "setup"],
        check=True,
        env=engine_environment(runtime),
    )
    return engine


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the headless engine inside the Super Speech skill"
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--agent",
        choices=("codex", "claude"),
        default="codex",
        help="agent skill directory to install into (default: codex)",
    )
    destination.add_argument(
        "--target",
        type=Path,
        help="explicit skill directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    validate_platform()
    destination = args.target or default_skill_directory(args.agent)
    engine = install(destination)
    print(f"Installed the Super Speech skill at {destination.expanduser().resolve()}")
    print(f"Installed its private headless engine at {engine}")


if __name__ == "__main__":
    main()
