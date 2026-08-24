from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
BUILD_DIR = APP_DIR / ".engine-build"
DIST_DIR = APP_DIR / ".engine-dist"
RESOURCE_DIR = APP_DIR / "build-resources" / "engine"


def remove_directory(directory: Path) -> None:
    directory = directory.resolve()
    if APP_DIR.resolve() not in directory.parents:
        raise RuntimeError(f"refusing to remove path outside app directory: {directory}")
    if directory.exists():
        shutil.rmtree(directory)


def main() -> None:
    for directory in (BUILD_DIR, DIST_DIR, RESOURCE_DIR):
        remove_directory(directory)
    BUILD_DIR.mkdir(parents=True)
    DIST_DIR.mkdir(parents=True)
    RESOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            "super-speech-engine",
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(BUILD_DIR / "work"),
            "--specpath",
            str(BUILD_DIR),
            "--collect-all",
            "kokoro_onnx",
            "--collect-all",
            "sounddevice",
            "--collect-all",
            "espeakng_loader",
            "--collect-all",
            "phonemizer",
            "--collect-all",
            "language_tags",
            "--copy-metadata",
            "kokoro-onnx",
            "--copy-metadata",
            "phonemizer-fork",
            "--copy-metadata",
            "sounddevice",
            "--copy-metadata",
            "onnxruntime",
            str(REPO_ROOT / "super_speech_engine.py"),
        ],
        cwd=REPO_ROOT,
        check=True,
    )

    built = DIST_DIR / "super-speech-engine"
    executable = built / (
        "super-speech-engine.exe" if sys.platform == "win32" else "super-speech-engine"
    )
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not create {executable}")
    shutil.move(str(built), str(RESOURCE_DIR))
    print(RESOURCE_DIR)


if __name__ == "__main__":
    main()
