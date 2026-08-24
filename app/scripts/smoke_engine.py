from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
ENGINE = APP_DIR / "build-resources" / "engine" / (
    "super-speech-engine.exe" if sys.platform == "win32" else "super-speech-engine"
)
MODELS = APP_DIR / "build-resources" / "models" / "kokoro"


def main() -> None:
    if not ENGINE.is_file():
        raise RuntimeError(f"missing frozen engine: {ENGINE}")
    if not (MODELS / "kokoro-v1.0.onnx").is_file():
        raise RuntimeError(f"missing staged model: {MODELS}")

    with tempfile.TemporaryDirectory(prefix="super-speech-engine-smoke-") as temporary:
        runtime = Path(temporary)
        environment = {
            **os.environ,
            "SUPER_SPEECH_HOME": str(runtime),
            "SUPER_SPEECH_MODEL_DIR": str(MODELS),
            "SUPER_SPEECH_SILENT": "1",
        }
        subprocess.run(
            [
                str(ENGINE),
                "speak",
                "The frozen Super Speech engine is complete and working.",
                "--voice",
                "af_heart",
                "--gap-ms",
                "0",
            ],
            env=environment,
            check=True,
        )
        spoken = runtime / "spoken" / "001-af_heart-g0-say.txt"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if spoken.is_file():
                break
            time.sleep(0.25)

        if not spoken.is_file():
            raise RuntimeError("frozen engine did not drain its self-started queue")
        subprocess.run([str(ENGINE), "interrupt"], env=environment, check=True)
        stopped_deadline = time.monotonic() + 10
        while time.monotonic() < stopped_deadline:
            status = subprocess.run(
                [str(ENGINE), "status"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            if '"state": "stopped"' in status.stdout:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("frozen engine did not stop after interrupt")


if __name__ == "__main__":
    main()
