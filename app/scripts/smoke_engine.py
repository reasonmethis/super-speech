from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def read_status(environment: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        [str(ENGINE), "status"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    status = json.loads(result.stdout)
    if (
        status.get("version") != 14
        or not isinstance(status.get("timeline_revision"), int)
        or "filename" in result.stdout
    ):
        raise RuntimeError("frozen engine exposed an invalid public status shape")
    return status


def wait_for_status(
    environment: dict[str, str],
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float = 45.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = read_status(environment)
        if predicate(status):
            return status
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for frozen engine status")


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
        status = read_status(environment)
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
        inbox = runtime / "agent-inbox.jsonl"
        environment = {
            **os.environ,
            "SUPER_SPEECH_HOME": str(runtime),
            "SUPER_SPEECH_MODEL_DIR": str(MODELS),
            "SUPER_SPEECH_SILENT": "1",
        }
        try:
            speak_result = subprocess.run(
                [
                    str(ENGINE),
                    "speak",
                    (
                        "The frozen Super Speech engine is complete and working. "
                        "This longer fixture stays active while the replay test observes "
                        "Current speech and pauses it before silent playback finishes."
                    ),
                    "--voice",
                    "af_heart",
                    "--gap-ms",
                    "0",
                    "--inbox",
                    str(inbox),
                ],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            speechicle_id = speak_result.stdout.strip()
            if not speechicle_id.startswith("sp_"):
                raise RuntimeError("frozen engine returned an invalid Speechicle ID")
            spoken_status = wait_for_status(
                environment,
                lambda status: status.get("state") == "idle"
                and any(
                    item.get("id") == speechicle_id
                    for item in status.get("history", [])
                ),
            )
            spoken_item = next(
                item
                for item in spoken_status["history"]
                if item.get("id") == speechicle_id
            )
            if spoken_item.get("inbox") != str(inbox):
                raise RuntimeError("frozen engine lost Speechicle inbox metadata")
            spoken_files = list((runtime / "spoken").glob("*.txt"))
            if len(spoken_files) != 1:
                raise RuntimeError("frozen engine did not archive exactly one Speechicle")
            spoken = spoken_files[0]
            original_text = spoken.read_text(encoding="utf-8")
            replay_result = subprocess.run(
                [str(ENGINE), "play", speechicle_id],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            replay_payload = json.loads(replay_result.stdout)
            if (
                replay_payload.get("outcome") != "committed"
                or replay_payload.get("result_id") != speechicle_id
                or replay_payload.get("snapshot", {}).get("version") != 14
            ):
                raise RuntimeError(
                    "frozen engine returned an invalid replay result"
                )
            wait_for_status(
                environment,
                lambda status: (status.get("current") or {}).get("id")
                == speechicle_id,
            )
            subprocess.run([str(ENGINE), "pause"], env=environment, check=True)
            wait_for_status(
                environment,
                lambda status: status.get("state") == "paused"
                and (status.get("current") or {}).get("id") == speechicle_id,
            )
            subprocess.run([str(ENGINE), "resume"], env=environment, check=True)
            wait_for_status(
                environment,
                lambda status: status.get("state") == "idle"
                and status.get("current") is None
                and any(
                    item.get("id") == speechicle_id
                    for item in status.get("history", [])
                ),
            )
            if spoken.read_text(encoding="utf-8") != original_text:
                raise RuntimeError("frozen engine replay changed the archived text")
            if len(list((runtime / "spoken").glob("*.txt"))) != 1:
                raise RuntimeError("frozen engine replay duplicated the history entry")

            subprocess.run([str(ENGINE), "pause"], env=environment, check=True)
            for text in ("Queue first.", "Queue second.", "Queue third."):
                subprocess.run(
                    [
                        str(ENGINE),
                        "speak",
                        text,
                        "--voice",
                        "af_heart",
                        "--gap-ms",
                        "0",
                    ],
                    env=environment,
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
            queued_status = wait_for_status(
                environment,
                lambda status: status.get("current") is not None
                and status.get("queue_count", 0) >= 2,
            )
            queue_items = queued_status["queue"]
            source_id = queue_items[-1]["id"]
            before_id = queue_items[0]["id"]
            subprocess.run(
                [str(ENGINE), "move", source_id, before_id],
                env=environment,
                check=True,
            )
            moved_status = wait_for_status(
                environment,
                lambda status: status.get("queue")
                and status["queue"][0].get("id") == source_id,
            )
            if moved_status["queue"][0]["id"] != source_id:
                raise RuntimeError("frozen engine did not persist queue order")

            subprocess.run(
                [str(ENGINE), "archive", source_id],
                env=environment,
                check=True,
            )
            wait_for_status(
                environment,
                lambda status: all(
                    item.get("id") != source_id for item in status.get("queue", [])
                )
                and any(
                    item.get("id") == source_id for item in status.get("history", [])
                ),
            )
        finally:
            stop_engine(environment)


if __name__ == "__main__":
    main()
