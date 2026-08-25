from __future__ import annotations

import json
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


def process_exists(process_id: int | None) -> bool:
    if not process_id:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = open_process(0x1000, False, process_id)
        if not handle:
            return False
        close_handle(handle)
        return True
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def wait_for_file(path: Path, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {path.name}")


def stop_engine(environment: dict[str, str]) -> None:
    subprocess.run(
        [str(ENGINE), "interrupt"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    stopped_deadline = time.monotonic() + 10
    while time.monotonic() < stopped_deadline:
        status_result = subprocess.run(
            [str(ENGINE), "status"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        status = json.loads(status_result.stdout)
        engine_pid = status.get("engine_pid")
        if status.get("state") == "stopped" and not process_exists(engine_pid):
            return
        time.sleep(0.25)
    raise RuntimeError("frozen engine did not stop after interrupt")


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
        try:
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
            wait_for_file(spoken)
            replay_result = subprocess.run(
                [str(ENGINE), "play", spoken.stem],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            replay_id = json.loads(replay_result.stdout).get("id")
            if not isinstance(replay_id, str):
                raise RuntimeError(
                    "frozen engine returned an invalid replay acknowledgement"
                )
            replayed = runtime / "spoken" / f"{replay_id}.txt"
            wait_for_file(replayed)
            if spoken.read_text(encoding="utf-8") != replayed.read_text(encoding="utf-8"):
                raise RuntimeError("frozen engine replay changed the archived text")
        finally:
            stop_engine(environment)


if __name__ == "__main__":
    main()
