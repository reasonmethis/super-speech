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
                "--enqueue",
                "The frozen Super Speech engine is complete and working.",
                "--voice",
                "af_heart",
                "--gap-ms",
                "0",
            ],
            env=environment,
            check=True,
        )
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(
            [str(ENGINE)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creation_flags,
            start_new_session=sys.platform != "win32",
        )
        spoken = runtime / "spoken" / "001-af_heart-g0-say.txt"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and process.poll() is None:
            if spoken.is_file():
                break
            time.sleep(0.25)

        (runtime / "INTERRUPT").touch()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise RuntimeError("frozen engine did not stop after INTERRUPT")

        if process.returncode != 0 or not spoken.is_file():
            raise RuntimeError(
                f"frozen engine smoke test failed ({process.returncode})\n{stdout}\n{stderr}"
            )
        print(stdout, end="")
        if stderr:
            print(stderr, file=sys.stderr, end="")


if __name__ == "__main__":
    main()
