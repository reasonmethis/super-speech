#!/usr/bin/env python3
"""Local Super Speech engine and command-line interface.

One stored reply is a Speechicle. Older CLI options and internal names call it
a chunk. A piece is a smaller, sentence-sized part used only for synthesis and
playback.

A background worker prepares pieces in order and puts their audio in a bounded
buffer. The main thread plays that audio through sounddevice while checking
control signals about every 20 ms. A long Speechicle can start after its first
piece is ready instead of waiting for every piece to be synthesized.

Pieces in one Speechicle are prepared and played in order. If the next piece is
not ready, playback waits at that sentence boundary. The configured gap applies
only before the first piece.

The engine owns every playback and timeline command. Headless callers use the
CLI. The desktop uses the running engine's authenticated local control endpoint
so it does not start a Python process for each click. Both paths call the same
command functions. The first Speechicle in Queue is Current. Signal files in
BASE are the engine's private process protocol:
  PAUSE      - pause immediately; keep the current sample position until removed
  STOP       - finish Current if playback began, or exit before it starts
  INTERRUPT  - stop playback immediately and exit
  SKIP       - stop Current and its remaining pieces, archive it,
               continue with next
  CONTINUE   - cancel a graceful stop because new speech was queued
  MUTATION.*.json - enqueue, play, move, archive, delete, or clear the timeline
                    in one durable FIFO stream
  WARMUP     - synthesize a throwaway phrase to pay the first-inference cost

Env:
  SUPER_SPEECH_HOME        - override the runtime home directory
  SUPER_SPEECH_MODEL_DIR   - override the read-only Kokoro model directory
  SUPER_SPEECH_SILENT      - opt-in: preserve timing without making sound
  SUPER_SPEECH_SPLIT_CHARS - internal synthesis-piece target size; 0 disables splitting
"""
import argparse
import hashlib
import json
import math
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, NamedTuple, assert_never

from engine_control import EngineControlServer, LivePlaybackControl, PlaybackState
from file_lock import InterprocessFileLock
from inbox_listener import listen_inbox

from mutation_protocol import (
    ArchiveMutation,
    ClearMutation,
    DeleteMutation,
    EnqueueMutation,
    MoveMutation,
    MutationRequest,
    PlayMutation,
    mutation_payload,
    parse_cli_mutation,
    parse_durable_mutation,
    validate_request_id,
)
from pauseable_audio import PauseableAudio
from speechicle_identity import (
    SpeechicleFilename,
    is_public_id,
)
from super_speech_version import __version__ as ENGINE_VERSION
from timeline_storage import (
    MutationOutcomeUnconfirmed,
    TimelinePaths,
    TimelineStorage,
    normalize_inbox_path,
    normalize_source_label,
    replace_path_with_confirmation,
    stored_inbox_path_is_valid,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_USER_HOME = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
BASE = Path(os.environ.get("SUPER_SPEECH_HOME") or (_USER_HOME / ".super-speech"))
TIMELINE_PATHS = TimelinePaths(BASE)
QUEUE = TIMELINE_PATHS.queue
SPOKEN = TIMELINE_PATHS.history
FAILED = TIMELINE_PATHS.failed
LOG = BASE / "log.txt"

STOP = BASE / "STOP"
PAUSE = BASE / "PAUSE"
INTERRUPT = BASE / "INTERRUPT"
SKIP = BASE / "SKIP"
CONTINUE = BASE / "CONTINUE"
MUTATION = BASE / "MUTATION.json"
WARMUP = BASE / "WARMUP"
HEARTBEAT = BASE / "engine.alive"
STATUS = BASE / "status.json"
STATUS_FAILURE = BASE / "status.failed"
STORAGE_READY = BASE / "storage-ready.json"
INSTANCE_LOCK = BASE / "engine.lock"
PLAYBACK_COMMAND_LOCK = BASE / "playback-command.lock"
PLAYBACK_COMMAND_SEQUENCE = BASE / "playback-command-sequence.json"


def default_model_directory() -> Path:
    """Return the bundled model directory or the headless runtime location."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "models" / "kokoro"
    return BASE / "models" / "kokoro"


MODEL_DIR = Path(os.environ.get("SUPER_SPEECH_MODEL_DIR") or default_model_directory())
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

MODEL_RELEASE_ROOT = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0"
)
MODEL_ARTIFACTS = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}

DEFAULT_VOICE = "af_bella"
AVAILABLE_VOICES: set[str] = set()  # populated in main() once kokoro loads
POLL_INTERVAL = 0.2   # idle poll cadence
SIGNAL_TICK = 0.02    # match the output block for responsive playback controls
CHUNK_GAP_S = 0.2     # silence before each chunk (natural rhythm); override per-file with -gMMM-
BUFFER_MAX = 8        # pieces of pre-rendered audio the worker may bank ahead
HISTORY_LIMIT = 50    # recent spoken entries published to desktop clients
SPLIT_CHARS = int(os.environ.get("SUPER_SPEECH_SPLIT_CHARS", "250"))

SILENT = bool(os.environ.get("SUPER_SPEECH_SILENT"))

STATUS_VERSION = 16
STARTUP_TIMEOUT = 120.0

timeline = TimelineStorage(TIMELINE_PATHS, DEFAULT_VOICE)
playback_control = LivePlaybackControl(lambda: PAUSE.exists())
_playback_marker_executor: ThreadPoolExecutor | None = None


def _persist_playback_command(command_token: object, paused: bool) -> None:
    try:
        if paused:
            publish_ordered_marker(PAUSE)
        else:
            resume()
    finally:
        playback_control.end_command(command_token)


def _report_playback_persistence(future: Future[None]) -> None:
    try:
        future.result()
    except Exception as error:
        log(f"could not persist live playback command: {error}")


def _schedule_playback_persistence(command_token: object, paused: bool) -> None:
    executor = _playback_marker_executor
    if executor is None:
        _persist_playback_command(command_token, paused)
        return
    try:
        future = executor.submit(
            _persist_playback_command,
            command_token,
            paused,
        )
    except RuntimeError:
        playback_control.end_command(command_token)
        raise
    future.add_done_callback(_report_playback_persistence)


class EngineInstanceLock(InterprocessFileLock):
    """Hold the process lock that makes the engine single-instance."""

    def __init__(self) -> None:
        super().__init__(INSTANCE_LOCK)


@contextmanager
def playback_command_lock(timeout: float = 10.0):
    """Serialize publication and acceptance of ordered playback commands."""
    lock = InterprocessFileLock(PLAYBACK_COMMAND_LOCK)
    deadline = time.monotonic() + timeout
    try:
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for the playback command lock")
            time.sleep(0.01)
        yield
    finally:
        lock.release()


def engine_is_running() -> bool:
    """Return whether another process currently owns the engine lock."""
    probe = EngineInstanceLock()
    if not probe.acquire():
        return True
    probe.release()
    return False


def process_exists(process_id: object) -> bool:
    if not isinstance(process_id, int) or process_id <= 0:
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
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def engine_command(*arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, str(Path(__file__).resolve()), *arguments]


def start_engine() -> None:
    """Start the same engine executable detached and wait for its process lock."""
    if engine_is_running():
        if wait_for_engine_status(timeout=STARTUP_TIMEOUT):
            return
        if engine_is_running():
            try:
                stored_status = json.loads(STATUS.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stored_status = {}
            version = stored_status.get("version")
            status_pid = stored_status.get("engine_pid")
            if (
                version is not None
                and version != STATUS_VERSION
                and process_exists(status_pid)
            ):
                raise RuntimeError(
                    f"running engine uses unsupported protocol version {version}; "
                    "interrupt it and retry"
                )
            raise RuntimeError(
                f"engine lock is held but startup did not finish; inspect {LOG}"
            )
    if not MODEL_PATH.is_file() or not VOICES_PATH.is_file():
        raise RuntimeError(
            f"missing Kokoro models at {MODEL_DIR}; run 'super-speech-engine setup'"
        )

    BASE.mkdir(parents=True, exist_ok=True)
    # Wait until this child finishes startup cleanup so a new PLAY is not cleared as stale
    with LOG.open("a", encoding="utf-8") as engine_log:
        options: dict[str, object] = {
            "cwd": str(BASE),
            "stdin": subprocess.DEVNULL,
            "stdout": engine_log,
            "stderr": engine_log,
        }
        if os.name == "nt":
            options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(engine_command("serve"), **options)

    if wait_for_engine_status(process.pid, process, timeout=STARTUP_TIMEOUT):
        return
    raise RuntimeError(f"engine failed to start; inspect {LOG}")


def wait_for_queue_acceptance(timeout: float = 15.0) -> bool:
    """Return whether a live engine consumed the new-work notification."""
    deadline = time.monotonic() + timeout
    last_error: RuntimeError | None = None
    while True:
        try:
            start_engine()
        except RuntimeError as error:
            last_error = error
        running = engine_is_running()
        if not CONTINUE.exists() and running:
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if last_error is not None:
        log(f"speech remains queued after engine startup failure: {last_error}")
    return False


def wait_for_engine_status(
    expected_pid: int | None = None,
    process: subprocess.Popen | None = None,
    *,
    timeout: float = 5.0,
) -> bool:
    """Wait until the lock owner finishes storage preparation."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        running = engine_is_running()
        if running:
            try:
                status = json.loads(STATUS.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                status = {}
            updated_at = status.get("updated_at")
            pid_matches = expected_pid is None or status.get("engine_pid") == expected_pid
            owner_is_live = expected_pid is not None or process_exists(
                status.get("engine_pid")
            )
            if (
                _snapshot_is_valid(status)
                and pid_matches
                and owner_is_live
                and isinstance(updated_at, (int, float))
                and storage_is_ready(status.get("engine_pid"))
            ):
                return True
        elif expected_pid is None and process is None:
            return False
        if process is not None and process.poll() is not None:
            if engine_is_running():
                expected_pid = None
                process = None
                continue
            break
        time.sleep(0.05)
    return False


def storage_is_ready(engine_pid: object) -> bool:
    if not isinstance(engine_pid, int) or engine_pid <= 0:
        return False
    try:
        payload = json.loads(STORAGE_READY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return payload == {"engine_pid": engine_pid}


def publish_startup_json(
    target: Path,
    payload: dict[str, object],
    label: str,
    timeout: float = 5.0,
) -> None:
    """Retry a startup JSON write for a few seconds after an OS error."""
    started = time.monotonic()
    first_error: OSError | None = None
    while True:
        temporary = target.with_name(
            f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(temporary, target)
            if first_error is not None:
                log(
                    f"{label} publication recovered after "
                    f"{time.monotonic() - started:.1f} seconds"
                )
            return
        except OSError as error:
            if first_error is None:
                first_error = error
                log(f"{label} publication waiting: {type(error).__name__}: {error}")
            if time.monotonic() - started >= timeout:
                raise
            heartbeat(force=True)
            time.sleep(0.05)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def publish_storage_ready() -> None:
    publish_startup_json(
        STORAGE_READY,
        {"engine_pid": os.getpid()},
        "storage-ready marker",
    )


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install_models(destination: Path = MODEL_DIR) -> None:
    """Download and verify the two model artifacts needed by the engine."""
    destination.mkdir(parents=True, exist_ok=True)
    for name, expected_hash in MODEL_ARTIFACTS.items():
        target = destination / name
        if target.is_file() and file_hash(target) == expected_hash:
            print(f"verified {target}")
            continue

        partial = target.with_suffix(f"{target.suffix}.partial")
        partial.unlink(missing_ok=True)
        try:
            with urllib.request.urlopen(f"{MODEL_RELEASE_ROOT}/{name}") as response:
                with partial.open("wb") as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
            actual_hash = file_hash(partial)
            if actual_hash != expected_hash:
                raise RuntimeError(
                    f"hash mismatch for {name}: expected {expected_hash}, got {actual_hash}"
                )
            os.replace(partial, target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        print(f"installed {target}")


def public_id_for_path(path: Path) -> str:
    return timeline.public_id(path)


def _speechicle_status_item(path: Path, text: str) -> dict[str, object]:
    speechicle_id = public_id_for_path(path)
    item: dict[str, object] = {
        "id": speechicle_id,
        "text": text,
        "voice": voice_from_name(path.name),
    }
    metadata = timeline.metadata(speechicle_id)
    if metadata.source is not None:
        item["source"] = metadata.source
    if metadata.inbox is not None:
        item["inbox"] = metadata.inbox
    return item


def queue_files_in_order() -> list[Path]:
    return timeline.queue_files()


def enqueue_text(
    text: str,
    voice: str,
    gap_ms: int | None = None,
    source: str | None = None,
    inbox: str | None = None,
) -> Path:
    """Atomically append one Speechicle to Queue."""
    text = text.strip()
    if not text:
        raise ValueError("speech text cannot be empty")
    if not re.fullmatch(r"[ab][fm]_[a-z0-9_]+", voice):
        raise ValueError(f"invalid Kokoro voice: {voice}")
    if gap_ms is not None and not 0 <= gap_ms <= 1500:
        raise ValueError("gap must be between 0 and 1500 milliseconds")
    return timeline.reserve(
        voice,
        gap_ms,
        text,
        normalize_source_label(source),
        normalize_inbox_path(inbox),
    )


def _normalize_cli_voice(voice: str) -> str:
    # Shell commands copied from Markdown can retain escaped underscores
    return voice.replace(r"\_", "_")


def history_snapshot() -> tuple[int, list[dict[str, object]]]:
    return timeline.history_snapshot(HISTORY_LIMIT)


def prepare_timeline_storage(instance_lock: EngineInstanceLock) -> None:
    recovered = timeline.prepare(instance_lock)
    if recovered is not None:
        log(f"recovered pending timeline {recovered} transaction")


def _archive_many(paths: list[Path]) -> bool:
    return timeline.archive_many(paths)


def archive(path: Path) -> bool:
    return _archive_many([path])


def archive_failed(path: Path) -> bool:
    try:
        timeline.archive_failed(path)
        return True
    except (OSError, RuntimeError) as error:
        log(f"archive_failed error {path.name}: {error}")
        return False


def log(msg: str, *, stderr: bool = False) -> None:
    now = time.time()
    line = f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d} {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr if stderr else sys.stdout, flush=True)


_last_hb = 0.0


def heartbeat(force: bool = False) -> None:
    """Publish cheap liveness while the engine may be busy synthesizing."""
    global _last_hb
    now = time.time()
    if force or now - _last_hb >= 1.0:
        try:
            HEARTBEAT.touch()
        except Exception:
            pass
        _last_hb = now


def voice_from_name(name: str) -> str:
    try:
        voice = SpeechicleFilename.parse(name).voice
    except ValueError as error:
        raise RuntimeError(f"speech filename is not canonical: {name}") from error
    if AVAILABLE_VOICES and voice not in AVAILABLE_VOICES:
        log(f"unknown voice {voice!r} in {name}; falling back to {DEFAULT_VOICE}")
        return DEFAULT_VOICE
    return voice


def gap_from_name(name: str) -> float | None:
    """Return the canonical per-Speechicle gap in seconds, if present."""
    try:
        gap_ms = SpeechicleFilename.parse(name).gap_ms
    except ValueError as error:
        raise RuntimeError(f"speech filename is not canonical: {name}") from error
    return gap_ms / 1000.0 if gap_ms is not None else None


_SENT_RE = re.compile(r"(?<=[.!?…])\s+")


FIRST_PIECE_CHARS = 120  # first-piece target; one long sentence may exceed it


class SpeechPiece(NamedTuple):
    text: str
    start: int
    end: int


def split_text_pieces(text: str, target: int) -> list[SpeechPiece]:
    """Pack sentences while retaining each piece's range in the source text."""
    if target <= 0:
        return [SpeechPiece(text, 0, len(text))]

    sentences: list[SpeechPiece] = []
    cursor = 0
    for separator in [*_SENT_RE.finditer(text), None]:
        boundary = separator.start() if separator is not None else len(text)
        raw = text[cursor:boundary]
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        if trailing > leading:
            start = cursor + leading
            end = cursor + trailing
            sentences.append(SpeechPiece(text[start:end], start, end))
        cursor = separator.end() if separator is not None else len(text)
    if not sentences:
        return [SpeechPiece(text, 0, len(text))]

    pieces: list[SpeechPiece] = []
    current: list[SpeechPiece] = []
    current_length = 0
    for sentence in sentences:
        cap = min(FIRST_PIECE_CHARS, target) if not pieces else target
        joined_length = current_length + (1 if current else 0) + len(sentence.text)
        if current and joined_length > cap:
            pieces.append(
                SpeechPiece(
                    " ".join(item.text for item in current),
                    current[0].start,
                    current[-1].end,
                )
            )
            current = [sentence]
            current_length = len(sentence.text)
        else:
            current.append(sentence)
            current_length = joined_length
    pieces.append(
        SpeechPiece(
            " ".join(item.text for item in current),
            current[0].start,
            current[-1].end,
        )
    )
    return pieces


def split_text(text: str, target: int) -> list[str]:
    """Return the exact strings synthesized for each internal speech piece."""
    return [piece.text for piece in split_text_pieces(text, target)]


def consume(signal: Path) -> bool:
    if not signal.exists():
        return False
    try:
        signal.unlink()
    except OSError:
        pass
    return True


def consume_control(signal: Path) -> bool:
    """Consume a control only when it belongs to this engine process.

    Empty files remain valid for compatibility with older clients. New clients
    include the lock owner's PID so a delayed command cannot affect its successor.
    """
    try:
        payload = signal.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if payload:
        try:
            target_pid = json.loads(payload).get("engine_pid")
        except (AttributeError, ValueError):
            target_pid = None
        if target_pid != os.getpid():
            signal.unlink(missing_ok=True)
            log(f"ignored stale {signal.name} for engine {target_pid}")
            return False
    signal.unlink(missing_ok=True)
    return True


@dataclass(frozen=True)
class OrderedMarker:
    sequence: int
    engine_pid: int | None


def _mutation_artifact_sequence(path: Path) -> int | None:
    parts = path.name.split(".")
    if (
        len(parts) != 4
        or parts[0] != MUTATION.stem
        or parts[-1] not in {"json", "claim"}
    ):
        raise ValueError("invalid mutation filename")
    order_token = parts[1]
    if order_token.isdigit():
        return None
    match = re.fullmatch(r"s(\d{20,})", order_token)
    if match is None or int(match.group(1)) <= 0:
        raise ValueError("invalid mutation command sequence in filename")
    return int(match.group(1))


def _read_marker_unlocked(signal: Path) -> OrderedMarker | None:
    for attempt in range(3):
        try:
            text = signal.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            if attempt < 2:
                time.sleep(0.01)
                continue
            raise RuntimeError(f"{signal.name} marker is unavailable") from error
        break
    if text == "":
        return OrderedMarker(0, None)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{signal.name} marker is invalid") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{signal.name} marker is invalid")
    keys = set(payload)
    target_pid = payload.get("engine_pid")
    if "engine_pid" in payload and (
        not isinstance(target_pid, int)
        or isinstance(target_pid, bool)
        or target_pid <= 0
    ):
        raise RuntimeError(f"{signal.name} marker has an invalid engine PID")
    if keys == {"engine_pid"}:
        return OrderedMarker(0, target_pid)
    if keys not in ({"command_sequence"}, {"command_sequence", "engine_pid"}):
        raise RuntimeError(f"{signal.name} marker is invalid")
    sequence = payload.get("command_sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence <= 0
    ):
        raise RuntimeError(f"{signal.name} marker has an invalid command sequence")
    return OrderedMarker(sequence, target_pid)


def _recover_command_sequence_unlocked() -> int:
    recovered = 0
    for signal in (PAUSE, STOP, CONTINUE):
        marker = _read_marker_unlocked(signal)
        if marker is not None:
            recovered = max(recovered, marker.sequence)
    for pattern in (f"{MUTATION.stem}.*.json", f"{MUTATION.stem}.*.claim"):
        for artifact in BASE.glob(pattern):
            try:
                sequence = _mutation_artifact_sequence(artifact)
            except ValueError as error:
                raise RuntimeError(
                    "could not recover playback command sequence"
                ) from error
            if sequence is not None:
                recovered = max(recovered, sequence)
    return recovered


def _read_command_sequence_unlocked() -> int:
    try:
        payload = json.loads(PLAYBACK_COMMAND_SEQUENCE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _recover_command_sequence_unlocked()
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("playback command sequence is unavailable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("last_sequence"), int)
        or isinstance(payload.get("last_sequence"), bool)
        or payload["last_sequence"] < 0
    ):
        raise RuntimeError("playback command sequence is invalid")
    return payload["last_sequence"]


def _replace_command_json_unlocked(
    temporary: Path,
    target: Path,
    payload: dict[str, object],
    error_message: str,
) -> None:
    last_error: OSError | None = None
    for _ in range(5):
        try:
            os.replace(temporary, target)
            return
        except OSError as error:
            last_error = error
            try:
                stored = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            else:
                if stored == payload:
                    return
            time.sleep(0.02)
    raise RuntimeError(error_message) from last_error


def _allocate_command_sequence_unlocked() -> int:
    sequence = _read_command_sequence_unlocked() + 1
    payload: dict[str, object] = {"version": 1, "last_sequence": sequence}
    temporary = PLAYBACK_COMMAND_SEQUENCE.with_name(
        f".{PLAYBACK_COMMAND_SEQUENCE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        _replace_command_json_unlocked(
            temporary,
            PLAYBACK_COMMAND_SEQUENCE,
            payload,
            "could not advance playback command sequence",
        )
    finally:
        temporary.unlink(missing_ok=True)
    return sequence


def _ordered_marker_sequence_unlocked(signal: Path) -> int | None:
    marker = _read_marker_unlocked(signal)
    if marker is None:
        return None
    target_pid = marker.engine_pid
    if target_pid is not None and target_pid != os.getpid():
        signal.unlink(missing_ok=True)
        log(f"ignored stale {signal.name} for engine {target_pid}")
        return None
    return marker.sequence


def _publish_ordered_marker_unlocked(
    signal: Path,
    *,
    engine_pid: int | None = None,
) -> int:
    sequence = _allocate_command_sequence_unlocked()
    payload: dict[str, object] = {"command_sequence": sequence}
    if engine_pid is not None:
        payload["engine_pid"] = engine_pid
    temporary = signal.with_name(
        f".{signal.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        _replace_command_json_unlocked(
            temporary,
            signal,
            payload,
            f"could not publish {signal.name}",
        )
    finally:
        temporary.unlink(missing_ok=True)
    return sequence


def publish_ordered_marker(
    signal: Path,
    *,
    engine_pid: int | None = None,
) -> int:
    with playback_command_lock():
        return _publish_ordered_marker_unlocked(signal, engine_pid=engine_pid)


def remove_ordered_marker(signal: Path) -> int:
    """Publish an ordered removal, used by Resume."""
    with playback_command_lock():
        sequence = _allocate_command_sequence_unlocked()
        signal.unlink(missing_ok=True)
        return sequence


def mutation_result_path(request_id: str) -> Path:
    validate_request_id(request_id)
    return BASE / f"MUTATION_RESULT.{request_id}.json"


def _publish_mutation_unlocked(request: MutationRequest) -> None:
    sequence = _allocate_command_sequence_unlocked()
    request_path = MUTATION.with_name(
        f"{MUTATION.stem}.s{sequence:020d}.{request.request_id}.json"
    )
    temp_path = request_path.with_name(
        f"{request_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = mutation_payload(request)
    payload["command_sequence"] = sequence
    try:
        temp_path.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        _replace_command_json_unlocked(
            temp_path,
            request_path,
            payload,
            "could not publish timeline mutation",
        )
    finally:
        temp_path.unlink(missing_ok=True)


def request_mutation(request: MutationRequest) -> str:
    """Publish one mutation to the engine's durable FIFO stream."""
    if not engine_is_running():
        raise RuntimeError("engine is not running")

    BASE.mkdir(parents=True, exist_ok=True)
    with playback_command_lock():
        _publish_mutation_unlocked(request)
    return request.request_id


def build_mutation_request(mutation_type: str, **fields: object) -> MutationRequest:
    payload = {
        "request_id": secrets.token_hex(12),
        "type": mutation_type,
        **fields,
    }
    return parse_durable_mutation(payload)


def claim_next_mutation_request() -> Path | None:
    """Claim the oldest pending mutation so later requests stay unclaimed."""
    for request in sorted(BASE.glob(f"{MUTATION.stem}.*.json")):
        claim = request.with_suffix(".claim")
        try:
            replace_path_with_confirmation(
                request,
                claim,
                f"mutation claim {request.name}",
                missing_source_is_rejection=True,
            )
        except FileNotFoundError:
            continue
        except OSError as error:
            log(f"could not claim mutation {request.name}: {error}")
            return None
        return claim
    return None


def read_mutation_claim(claimed: Path) -> MutationRequest:
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read mutation: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid mutation JSON: {error}") from error
    request = parse_durable_mutation(payload)
    filename_sequence = _mutation_artifact_sequence(claimed)
    if filename_sequence != request.command_sequence:
        raise ValueError("mutation command sequence does not match its filename")
    return request


def request_id_from_claim_path(claimed: Path) -> str | None:
    parts = claimed.name.split(".")
    if len(parts) != 4 or parts[0] != MUTATION.stem or parts[-1] != "claim":
        return None
    try:
        return validate_request_id(parts[-2])
    except ValueError:
        return None


def _mutation_result_payload(
    request_id: str,
    outcome: str,
    snapshot: dict[str, object],
    *,
    result_id: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if outcome not in {"committed", "rejected", "unconfirmed"}:
        raise ValueError("invalid mutation outcome")
    if not _snapshot_is_valid(snapshot):
        raise ValueError("invalid mutation snapshot")
    if outcome == "committed" and error is not None:
        raise ValueError("committed mutation cannot contain an error")
    if outcome != "committed" and not error:
        raise ValueError("non-committed mutation requires an error")
    if outcome != "committed" and result_id is not None:
        raise ValueError("non-committed mutation cannot contain a result ID")
    payload: dict[str, object] = {
        "outcome": outcome,
        "request_id": request_id,
        "snapshot": snapshot,
    }
    if result_id is not None:
        if not is_public_id(result_id):
            raise ValueError("invalid mutation result ID")
        payload["result_id"] = result_id
    if error is not None:
        payload["error"] = error
    return payload


def publish_mutation_result(
    request_id: str,
    outcome: str,
    snapshot: dict[str, object],
    *,
    result_id: str | None = None,
    error: str | None = None,
) -> bool:
    payload = _mutation_result_payload(
        request_id,
        outcome,
        snapshot,
        result_id=result_id,
        error=error,
    )
    target = mutation_result_path(request_id)
    last_error: OSError | None = None
    for _ in range(5):
        temporary = target.with_name(
            f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, target)
            return True
        except OSError as error:
            last_error = error
            time.sleep(0.02)
        finally:
            temporary.unlink(missing_ok=True)
    log(f"could not publish mutation result: {last_error}")
    return False


def retire_claim(claimed: Path, result_published: bool) -> None:
    """Remove a settled claim or retain it for restart recovery."""
    if result_published:
        try:
            claimed.unlink(missing_ok=True)
        except OSError as error:
            log(f"could not remove settled mutation {claimed.name}: {error}")
        return


def cancel_unclaimed_mutation(request_id: str) -> bool:
    """Cancel a request only while the engine has not claimed it."""
    for _ in range(5):
        for request in BASE.glob(f"{MUTATION.stem}.*.{request_id}.json"):
            cancelled = request.with_suffix(".cancel")
            try:
                replace_path_with_confirmation(
                    request,
                    cancelled,
                    f"mutation cancellation {request.name}",
                    missing_source_is_rejection=True,
                )
            except FileNotFoundError:
                continue
            except (OSError, MutationOutcomeUnconfirmed):
                continue
            cancelled.unlink(missing_ok=True)
            return True
        time.sleep(0.02)
    return False


def mutation_is_unclaimed(request_id: str) -> bool:
    return any(BASE.glob(f"{MUTATION.stem}.*.{request_id}.json"))


def mutation_is_claimed(request_id: str) -> bool:
    return any(BASE.glob(f"{MUTATION.stem}.*.{request_id}.claim"))


def wait_for_mutation_payload(
    request_id: str, timeout: float
) -> dict[str, object] | None:
    target = mutation_result_path(request_id)
    payload = wait_for_json_payload(target, time.monotonic() + timeout)
    if payload is not None:
        return payload
    grace_period = min(max(timeout, 0.1), 5.0)
    unclaimed_deadline = time.monotonic() + grace_period
    while mutation_is_unclaimed(request_id) and time.monotonic() < unclaimed_deadline:
        if cancel_unclaimed_mutation(request_id):
            return None
        time.sleep(0.05)
    settlement_deadline = time.monotonic() + grace_period
    while mutation_is_claimed(request_id) and time.monotonic() < settlement_deadline:
        if not engine_is_running():
            try:
                start_engine()
            except RuntimeError:
                break
        payload = wait_for_json_payload(target, time.monotonic() + 1.0)
        if payload is not None:
            return payload
    return wait_for_json_payload(target, time.monotonic() + 0.1)


def wait_for_json_payload(target: Path, deadline: float) -> dict[str, object] | None:
    """Read one atomic result, tolerating short Windows file locks."""
    while time.monotonic() < deadline:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except OSError:
            time.sleep(0.05)
            continue
        except json.JSONDecodeError as error:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"invalid engine result: {error}") from error
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            log(
                f"could not remove result {target.name}: {error}",
                stderr=True,
            )
        if not isinstance(payload, dict):
            raise RuntimeError("invalid engine result: expected an object")
        return payload
    return None


def _source_label_is_valid(value: object) -> bool:
    try:
        return normalize_source_label(value) == value
    except ValueError:
        return False


def _status_item_is_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        is_public_id(value.get("id"))
        and "filename" not in value
        and isinstance(value.get("text"), str)
        and isinstance(value.get("voice"), str)
        and _source_label_is_valid(value.get("source"))
        and stored_inbox_path_is_valid(value.get("inbox"))
    )


def _current_status_item_is_valid(value: object) -> bool:
    if not _status_item_is_valid(value):
        return False
    assert isinstance(value, dict)
    piece = value.get("piece")
    piece_count = value.get("piece_count")
    elapsed = value.get("elapsed_seconds")
    if (
        not isinstance(piece, int)
        or isinstance(piece, bool)
        or not isinstance(piece_count, int)
        or isinstance(piece_count, bool)
        or piece < 0
        or piece_count < 1
        or piece > piece_count
        or not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
    ):
        return False
    start = value.get("piece_start")
    end = value.get("piece_end")
    if piece == 0:
        return start is None and end is None
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(value["text"])
    )


def _snapshot_is_valid(snapshot: object) -> bool:
    if not isinstance(snapshot, dict):
        return False
    current = snapshot.get("current")
    queue_items = snapshot.get("queue")
    history_items = snapshot.get("history")
    queue_count = snapshot.get("queue_count")
    history_count = snapshot.get("history_count")
    timeline_revision = snapshot.get("timeline_revision")
    updated_at = snapshot.get("updated_at")
    engine_pid = snapshot.get("engine_pid")
    state = snapshot.get("state")
    if not (
        snapshot.get("version") == STATUS_VERSION
        and state
        in {
            "clearing",
            "idle",
            "loading",
            "paused",
            "playing",
            "setup_required",
            "stopped",
        }
        and isinstance(timeline_revision, int)
        and not isinstance(timeline_revision, bool)
        and timeline_revision >= 0
        and isinstance(updated_at, (int, float))
        and not isinstance(updated_at, bool)
        and (
            engine_pid is None
            or (isinstance(engine_pid, int) and not isinstance(engine_pid, bool))
        )
        and (current is None or _current_status_item_is_valid(current))
        and isinstance(queue_items, list)
        and all(_status_item_is_valid(item) for item in queue_items)
        and isinstance(queue_count, int)
        and not isinstance(queue_count, bool)
        and queue_count == len(queue_items)
        and isinstance(history_items, list)
        and all(_status_item_is_valid(item) for item in history_items)
        and isinstance(history_count, int)
        and not isinstance(history_count, bool)
        and history_count >= len(history_items)
        and (current is not None or not queue_items)
        and (state not in {"clearing", "playing", "paused"} or current is not None)
        and (state != "idle" or current is None)
    ):
        return False
    active_ids = {item["id"] for item in queue_items}
    if isinstance(current, dict):
        active_ids.add(current["id"])
    history_ids = {item["id"] for item in history_items}
    return (
        len(active_ids) == len(queue_items) + (1 if current is not None else 0)
        and len(history_ids) == len(history_items)
        and active_ids.isdisjoint(history_ids)
    )


def _status_payload(
    *,
    timeline_revision: int,
    state: str,
    updated_at: float,
    engine_pid: int | None,
    current: dict[str, object] | None,
    queue_items: list[dict[str, object]],
    history_count: int,
    history_items: list[dict[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": STATUS_VERSION,
        "timeline_revision": timeline_revision,
        "state": state,
        "updated_at": updated_at,
        "engine_pid": engine_pid,
        "current": current,
        "queue_count": len(queue_items),
        "queue": queue_items,
        "history_count": history_count,
        "history": history_items,
    }
    if not _snapshot_is_valid(payload):
        raise ValueError("invalid engine status payload")
    return payload


def wait_for_mutation_result(
    request_id: str, timeout: float = 60.0
) -> dict[str, object]:
    payload = wait_for_mutation_payload(request_id, timeout)
    if payload is None:
        if mutation_is_claimed(request_id) or mutation_is_unclaimed(request_id):
            raise MutationOutcomeUnconfirmed("mutation result was unconfirmed")
        raise RuntimeError("engine did not publish a mutation result")
    if payload.get("request_id") != request_id:
        raise RuntimeError("engine returned a result for another mutation")
    if payload.get("outcome") not in {"committed", "rejected", "unconfirmed"}:
        raise RuntimeError("engine returned an invalid mutation outcome")
    if not _snapshot_is_valid(payload.get("snapshot")):
        raise RuntimeError("engine returned an invalid mutation snapshot")
    result_id = payload.get("result_id")
    if result_id is not None and not is_public_id(result_id):
        raise RuntimeError("engine returned an invalid mutation result ID")
    error = payload.get("error")
    if error is not None and not isinstance(error, str):
        raise RuntimeError("engine returned an invalid mutation error")
    return payload


def execute_mutation_request(request: MutationRequest) -> dict[str, object]:
    """Submit one parsed desktop mutation and return its authoritative result."""
    previous_snapshot = _read_authoritative_status()
    request_mutation(request)
    try:
        return wait_for_mutation_result(request.request_id)
    except MutationOutcomeUnconfirmed as error:
        try:
            snapshot = _read_authoritative_status()
        except RuntimeError:
            snapshot = previous_snapshot
        return _mutation_result_payload(
            request.request_id,
            "unconfirmed",
            snapshot,
            error=str(error),
        )


def execute_mutation(payload: object) -> dict[str, object]:
    """Parse and submit one desktop mutation."""
    request = parse_cli_mutation(payload, secrets.token_hex(12))
    return execute_mutation_request(request)


def playback_control_ack(
    paused: bool, audio_state: PlaybackState
) -> dict[str, object]:
    """Return a compact acknowledgement of the applied live audio state."""
    has_work = _read_authoritative_status().get("current") is not None
    return {
        "version": 1,
        "engine_pid": os.getpid(),
        "state": ("paused" if paused else "playing") if has_work else "idle",
        "updated_at": time.time(),
        "audio_state": audio_state,
    }


def execute_control_request(payload: dict[str, object]) -> object:
    """Apply one request received by the running engine control server."""
    command = payload.get("command")
    if command in ("pause", "resume") and set(payload) == {"command"}:
        paused = command == "pause"
        command_token, audio_state = playback_control.begin_command(paused)
        _schedule_playback_persistence(command_token, paused)
        return playback_control_ack(paused, audio_state)
    if command == "mutate" and set(payload) == {"command", "mutation"}:
        request = parse_cli_mutation(payload["mutation"], secrets.token_hex(12))
        if not isinstance(request, ClearMutation):
            return execute_mutation_request(request)
        playback_control.start_clearing(request.request_id)
        try:
            return execute_mutation_request(request)
        finally:
            playback_control.finish_clearing(
                request.request_id,
                hold_active=False,
            )
    raise ValueError("invalid engine control request")


def _read_authoritative_status(timeout: float = 1.0) -> dict[str, object]:
    """Read the latest complete status without consuming its shared file."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            snapshot = json.loads(STATUS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if (
            _snapshot_is_valid(snapshot)
            and not _contains_private_status_field(snapshot)
        ):
            assert isinstance(snapshot, dict)
            return snapshot
        time.sleep(0.05)
    raise RuntimeError("engine status is unavailable")


def prune_mutation_results(max_age: float = 300.0) -> None:
    cutoff = time.time() - max_age
    for result in BASE.glob("MUTATION_RESULT.*.json"):
        try:
            request_id = result.name.removeprefix("MUTATION_RESULT.").removesuffix(
                ".json"
            )
            if (
                result.stat().st_mtime < cutoff
                and not mutation_is_claimed(request_id)
                and not mutation_is_unclaimed(request_id)
            ):
                result.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            log(f"could not prune mutation result {result.name}: {error}")


def warmup(kokoro) -> None:
    """Attempt a throwaway synthesis to pay the one-time
    first-inference cost up front, so the first Speechicle renders fast."""
    heartbeat(force=True)
    t0 = time.time()
    try:
        kokoro.create("Warming up the model.", voice=DEFAULT_VOICE, speed=1.0, lang="en-us")
        log(f"warmup (discarded) synth={time.time()-t0:.1f}s")
    except Exception as e:
        log(f"warmup error: {e}")


@dataclass(frozen=True)
class ActivePiece:
    piece: int
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.piece <= 0 or self.start < 0 or self.end <= self.start:
            raise ValueError("invalid active piece progress")


@dataclass(frozen=True)
class CurrentProjection:
    """Cached playback details for the Speechicle at the playback boundary.

    `active_piece` is empty until the first piece starts. It then keeps the most
    recent piece while the engine waits for the next one. `skip_initial_gap`
    remembers that an explicit Play command should bypass the gap before a
    Speechicle begins.
    """

    filename: str
    text: str
    piece_count: int = field(init=False)
    active_piece: ActivePiece | None = None
    skip_initial_gap: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "piece_count", _piece_count(self.text))
        if self.active_piece is None:
            return
        if self.active_piece.piece > self.piece_count:
            raise ValueError("active piece exceeds piece count")
        if self.active_piece.end > len(self.text):
            raise ValueError("active piece exceeds Current text")


class BufferedPiece(NamedTuple):
    """One synthesized piece waiting for the player.

    `claim_generation` identifies the synthesis attempt that made the piece.
    The engine records only the current generation for each Speechicle. Removing
    or replacing that record cancels older work, so the player drops pieces whose
    generation no longer matches.
    """

    path: Path
    audio: object
    sample_rate: int
    is_first_piece: bool
    is_last_piece: bool
    piece_number: int
    speechicle_text: str
    piece_start: int
    piece_end: int
    claim_generation: int


def _current_status_item(
    path: Path,
    projection: CurrentProjection,
    elapsed_seconds: float = 0.0,
) -> dict[str, object]:
    """Build Current status fields shared by live and stopped snapshots."""
    active_piece = projection.active_piece
    item = _speechicle_status_item(path, projection.text)
    item.update({
        "piece": active_piece.piece if active_piece is not None else 0,
        "piece_count": projection.piece_count,
        "piece_start": active_piece.start if active_piece is not None else None,
        "piece_end": active_piece.end if active_piece is not None else None,
        "elapsed_seconds": elapsed_seconds,
    })
    return item


class State:
    """Shared coordination between the main (consumer) and worker (producer)."""

    def __init__(
        self,
        timeline_revision: int = 0,
        timeline_fingerprint: str | None = None,
    ) -> None:
        self.lock = threading.RLock()
        # Filename to generation for work being synthesized, buffered, or played
        self.claims: dict[str, int] = {}
        self.next_claim_generation = 0
        # Active playback-boundary item, including synthesis and inter-item gaps
        self.current_projection: CurrentProjection | None = None
        self.read_failures: dict[str, float] = {}
        self.stop = threading.Event()    # tell the worker to exit
        self.saw_stop = False            # graceful Stop; a newer accepted command may cancel it
        self.timeline_revision = timeline_revision
        self.timeline_fingerprint = timeline_fingerprint


def _piece_count(text: str) -> int:
    return len(split_text(text, SPLIT_CHARS))


def replace_current_projection(
    st: State,
    filename: str,
    text: str,
    *,
    skip_initial_gap: bool = False,
) -> None:
    """Replace Current after an authoritative timeline transition."""
    with st.lock:
        st.current_projection = CurrentProjection(
            filename,
            text,
            skip_initial_gap=skip_initial_gap,
        )


def start_current_playback(
    st: State,
    filename: str,
    text: str,
) -> bool:
    """Refresh Current for its first buffered piece without replacing another row."""
    with st.lock:
        if st.current_projection is not None and st.current_projection.filename != filename:
            return False
        st.current_projection = CurrentProjection(
            filename,
            text,
            skip_initial_gap=(
                st.current_projection.skip_initial_gap if st.current_projection is not None else False
            ),
        )
        return True


def update_current_piece(
    st: State,
    expected_filename: str,
    piece: int,
    piece_start: int,
    piece_end: int,
) -> bool:
    """Advance progress only while the buffered piece still owns Current."""
    with st.lock:
        current = st.current_projection
        if current is None or current.filename != expected_filename:
            return False
        st.current_projection = replace(
            current,
            active_piece=ActivePiece(piece, piece_start, piece_end),
        )
        return True


def consume_initial_gap_skip(st: State, expected_filename: str) -> bool:
    """Consume the one-shot gap skip attached to a selected Current row."""
    with st.lock:
        current = st.current_projection
        if (
            current is None
            or current.filename != expected_filename
            or not current.skip_initial_gap
        ):
            return False
        st.current_projection = replace(current, skip_initial_gap=False)
        return True


def clear_current_playback(st: State, expected_filename: str | None) -> bool:
    """Clear Current only if it still matches the observed filename."""
    with st.lock:
        current_filename = st.current_projection.filename if st.current_projection is not None else None
        if current_filename != expected_filename:
            return False
        st.current_projection = None
        return True


def _command_allowed_by_graceful_stop_unlocked(
    command_sequence: int | None,
    st: State,
) -> bool:
    """Return whether no graceful Stop is newer than this command."""
    stop_sequence = _ordered_marker_sequence_unlocked(STOP)
    if stop_sequence is None:
        return not st.saw_stop
    return command_sequence is not None and stop_sequence < command_sequence


def command_allowed_by_graceful_stop(
    command_sequence: int | None,
    st: State,
) -> bool:
    with playback_command_lock():
        return _command_allowed_by_graceful_stop_unlocked(command_sequence, st)


def _remove_marker_before_unlocked(
    signal: Path,
    command_sequence: int | None,
) -> bool:
    marker_sequence = _ordered_marker_sequence_unlocked(signal)
    if (
        marker_sequence is None
        or command_sequence is None
        or marker_sequence >= command_sequence
    ):
        return False
    signal.unlink(missing_ok=True)
    return True


def finalize_accepted_command(
    command_sequence: int | None,
    st: State,
    *,
    resume: bool = False,
) -> None:
    """Remove only markers older than one successfully committed command."""
    try:
        with playback_command_lock():
            if _remove_marker_before_unlocked(STOP, command_sequence):
                st.saw_stop = False
            if resume:
                _remove_marker_before_unlocked(PAUSE, command_sequence)
    except (OSError, RuntimeError) as error:
        raise MutationOutcomeUnconfirmed(
            "mutation committed but playback marker cleanup failed"
        ) from error


def ordered_marker_requested(signal: Path) -> bool:
    with playback_command_lock():
        return _ordered_marker_sequence_unlocked(signal) is not None


def consume_ordered_marker(signal: Path) -> bool:
    with playback_command_lock():
        if _ordered_marker_sequence_unlocked(signal) is None:
            return False
        signal.unlink(missing_ok=True)
        return True


def consume_continue(st: State) -> bool:
    """Consume new-work notice and cancel only an older graceful Stop."""
    with playback_command_lock():
        sequence = _ordered_marker_sequence_unlocked(CONTINUE)
        if sequence is None:
            return False
        accepted = _command_allowed_by_graceful_stop_unlocked(sequence, st)
        if accepted and _remove_marker_before_unlocked(STOP, sequence):
            st.saw_stop = False
        CONTINUE.unlink(missing_ok=True)
        return accepted


_last_status_write_monotonic = 0.0
_last_status_updated_at = 0.0
_status_failure_started: float | None = None


def _fingerprint_row(item: dict[str, object]) -> dict[str, object]:
    return {key: item.get(key) for key in ("id", "voice", "source")}


def timeline_fingerprint(
    current: dict[str, object] | None,
    queue_items: list[dict[str, object]],
    history_count: int,
    history_items: list[dict[str, object]],
) -> str:
    """Hash the ordered row metadata and History count shown to the user."""
    ordered_rows = {
        "current": _fingerprint_row(current) if current is not None else None,
        "queue": [_fingerprint_row(item) for item in queue_items],
        "history_count": history_count,
        "history": [_fingerprint_row(item) for item in history_items],
    }
    encoded = json.dumps(ordered_rows, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _advance_timeline_revision(st: State, fingerprint: str) -> int:
    """Update the one revision counter owned by status publication."""
    with st.lock:
        if st.timeline_fingerprint != fingerprint:
            if st.timeline_fingerprint is not None:
                st.timeline_revision += 1
            st.timeline_fingerprint = fingerprint
        return st.timeline_revision


def fingerprint_from_status(status: object) -> str | None:
    """Rebuild the persisted fingerprint from one valid status snapshot."""
    if not _snapshot_is_valid(status):
        return None
    assert isinstance(status, dict)
    return timeline_fingerprint(
        status["current"],
        status["queue"],
        status["history_count"],
        status["history"],
    )


def load_timeline_revision_seed() -> tuple[int, str | None]:
    """Load the last valid revision before the engine replaces its status file."""
    try:
        stored_status = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, None
    fingerprint = fingerprint_from_status(stored_status)
    if fingerprint is None:
        return 0, None
    assert isinstance(stored_status, dict)
    return stored_status["timeline_revision"], fingerprint


def publish_startup_status(timeline_revision: int) -> dict[str, object]:
    """Publish liveness before storage preparation reads the timeline."""
    previous: dict[str, object] | None = None
    try:
        candidate = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    else:
        if (
            fingerprint_from_status(candidate) is not None
            and not _contains_private_status_field(candidate)
        ):
            assert isinstance(candidate, dict)
            previous = candidate

    payload = _status_payload(
        timeline_revision=timeline_revision,
        state="loading",
        updated_at=time.time(),
        engine_pid=os.getpid(),
        current=(
            previous.get("current") if previous is not None else None
        ),
        queue_items=(
            previous.get("queue", []) if previous is not None else []
        ),
        history_count=(
            previous.get("history_count", 0) if previous is not None else 0
        ),
        history_items=(
            previous.get("history", []) if previous is not None else []
        ),
    )
    publish_startup_json(STATUS, payload, "startup status")
    heartbeat(force=True)
    return payload


LifecycleState = Literal["loading", "setup_required", "stopped", "clearing"]


def publish_status(
    st: State,
    *,
    lifecycle_state: LifecycleState | None = None,
    playback: PauseableAudio | None = None,
    sample_rate: int | None = None,
    force: bool = False,
) -> dict[str, object] | None:
    """Publish an atomic runtime snapshot for the desktop controller."""
    global _last_status_write_monotonic, _last_status_updated_at
    global _status_failure_started
    monotonic_now = time.monotonic()
    if not force and monotonic_now - _last_status_write_monotonic < 0.25:
        return None
    updated_at = max(
        time.time(),
        math.nextafter(_last_status_updated_at, math.inf),
    )

    with st.lock:
        try:
            ordered_queue = queue_files_in_order()
        except RuntimeError as error:
            log(str(error))
            return None
        current_path = ordered_queue[0] if ordered_queue else None
        if not st.stop.is_set():
            if current_path is None:
                st.current_projection = None
            elif (
                st.current_projection is None
                or st.current_projection.filename != current_path.name
            ):
                try:
                    text = current_path.read_text(encoding="utf-8").strip()
                except OSError:
                    text = ""
                st.current_projection = CurrentProjection(current_path.name, text)
        current_projection = st.current_projection
    queue_files = ordered_queue[1:]
    queue_items = []
    for path in queue_files:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        queue_items.append(_speechicle_status_item(path, text))

    current = None
    if current_path is not None:
        is_playing_path = (
            current_projection is not None
            and current_path.name == current_projection.filename
        )
        if not is_playing_path:
            try:
                current_text = current_path.read_text(encoding="utf-8").strip()
            except OSError:
                current_text = ""
            status_projection = CurrentProjection(
                current_path.name,
                current_text,
            )
        else:
            status_projection = current_projection
        current = _current_status_item(
            current_path,
            status_projection,
            (
                playback.position / sample_rate
                if is_playing_path and playback is not None and sample_rate
                else 0.0
            ),
        )

    has_work = current is not None
    if lifecycle_state == "clearing":
        state = "clearing" if has_work else "idle"
    elif lifecycle_state is None:
        state = (
            "paused" if has_work and playback_control.pause_requested()
            else "playing" if has_work
            else "idle"
        )
    else:
        state = lifecycle_state
    try:
        history_count, history_items = history_snapshot()
    except RuntimeError as error:
        log(str(error))
        return None
    fingerprint = timeline_fingerprint(
        current,
        queue_items,
        history_count,
        history_items,
    )
    timeline_revision = _advance_timeline_revision(st, fingerprint)

    payload = _status_payload(
        timeline_revision=timeline_revision,
        state=state,
        updated_at=updated_at,
        engine_pid=os.getpid(),
        current=current,
        queue_items=queue_items,
        history_count=history_count,
        history_items=history_items,
    )
    temp_path = STATUS.with_name(
        f"{STATUS.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, STATUS)
        STATUS_FAILURE.unlink(missing_ok=True)
        _last_status_write_monotonic = monotonic_now
        _last_status_updated_at = updated_at
        if _status_failure_started is not None:
            log(
                "status publication recovered after "
                f"{monotonic_now - _status_failure_started:.1f} seconds"
            )
        _status_failure_started = None
        return payload
    except OSError as error:
        if _status_failure_started is None:
            _status_failure_started = monotonic_now
            log(f"status publication failed: {type(error).__name__}: {error}")
        elif monotonic_now - _status_failure_started >= 5.0:
            try:
                STATUS_FAILURE.touch()
            except OSError as marker_error:
                log(f"could not publish status failure marker: {marker_error}")
            st.stop.set()
            log("stopping after status publication failed for 5 seconds")
        try:
            temp_path.unlink()
        except OSError:
            pass
    return None


def _discard_buffer(buf: "queue.Queue[BufferedPiece]") -> int:
    discarded = 0
    while True:
        try:
            buf.get_nowait()
            discarded += 1
        except queue.Empty:
            return discarded


def _reset_waiting_buffer(
    buf: "queue.Queue[BufferedPiece]",
    st: State,
    current_name: str,
) -> int:
    """Drop rendered Waiting audio while preserving captured Current pieces."""
    with st.lock:
        current_generation = st.claims.get(current_name)
        kept: list[BufferedPiece] = []
        discarded = 0
        while True:
            try:
                entry = buf.get_nowait()
            except queue.Empty:
                break
            if entry.path.name == current_name:
                kept.append(entry)
            else:
                discarded += 1
        for entry in kept:
            buf.put_nowait(entry)
        st.claims = (
            {current_name: current_generation}
            if current_generation is not None
            else {}
        )
        return discarded


def apply_play_mutation(
    buf: "queue.Queue[BufferedPiece]",
    st: State,
    request: PlayMutation,
) -> Literal["select"] | None:
    """Select one ID while excluding worker claims from the transaction."""
    with st.lock:
        chunk_id = request.id
        requested_voice = request.voice
        if requested_voice is not None and (
            AVAILABLE_VOICES and requested_voice not in AVAILABLE_VOICES
        ):
            raise ValueError(f"unknown Kokoro voice: {requested_voice}")
        try:
            selection = timeline.select(chunk_id, requested_voice)
        except MutationOutcomeUnconfirmed:
            raise
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"could not select {chunk_id}") from error
        if not selection.restart_playback:
            log(f"PLAY current {chunk_id}; accepted")
            return None
        target = selection.target

        try:
            selected_text = target.read_text(encoding="utf-8").strip()
        except OSError:
            selected_text = ""
        if not target.exists():
            raise MutationOutcomeUnconfirmed("play command result was unconfirmed")
        discarded = _discard_buffer(buf)
        st.claims.clear()
        replace_current_projection(
            st,
            target.name,
            selected_text,
            skip_initial_gap=True,
        )
        st.saw_stop = False
        if public_id_for_path(target) != chunk_id:
            raise MutationOutcomeUnconfirmed("play changed the stable Speechicle ID")
        if selection.origin != "history":
            log(
                f"PLAY selected {target.name}; archived {selection.moved_count} older Speechicle(s); "
                f"discarded {discarded} banked piece(s)"
            )
        else:
            log(
                f"PLAY History {chunk_id} as {target.name}; "
                f"promoted {selection.moved_count} item(s); "
                f"discarded {discarded} banked piece(s)"
            )
        return "select"


def apply_enqueue_mutation(request: EnqueueMutation) -> Path:
    """Append text through the same Queue path used by the public CLI."""
    if AVAILABLE_VOICES and request.voice not in AVAILABLE_VOICES:
        raise ValueError(f"unknown Kokoro voice: {request.voice}")
    return enqueue_text(
        request.text,
        request.voice,
        source=request.source,
        inbox=request.inbox,
    )


def do_clear(buf: "queue.Queue[BufferedPiece]", st: State) -> bool:
    """Stop and archive Current plus every Waiting row as one transaction."""
    with st.lock:
        if st.stop.is_set():
            return False
        discarded = _discard_buffer(buf)
        try:
            ordered = queue_files_in_order()
        except RuntimeError as error:
            st.claims.clear()
            st.stop.set()
            log(f"clear stopped because the saved queue order is unavailable: {error}")
            return False
        try:
            archived = _archive_many(ordered)
        except MutationOutcomeUnconfirmed as error:
            st.claims.clear()
            st.stop.set()
            log(f"clear result is unconfirmed; stopping: {error}")
            return False
        except (OSError, RuntimeError) as error:
            st.claims.clear()
            st.stop.set()
            log(f"clear could not recover the timeline; stopping: {error}")
            return False
        if not archived:
            st.claims.clear()
            st.stop.set()
            log("clear could not archive the timeline; stopping")
            return False
        st.claims.clear()
        current_name = st.current_projection.filename if st.current_projection is not None else None
        clear_current_playback(st, current_name)
    log(f"clear; archived {len(ordered)} row(s), dropped {discarded} piece(s)")
    return True


def apply_delete_mutation(request: DeleteMutation) -> None:
    """Delete one History row without disturbing the active queue."""
    history_item = timeline.delete_history(request.id)
    if history_item is None:
        log(f"QUEUE delete {request.id}; already absent")
        return
    log(f"QUEUE delete {history_item.name}")


def apply_history_move_mutation(request: MoveMutation) -> None:
    """Move one History row within the saved visible order."""
    source = timeline.reorder_history(request.id, request.before_id, HISTORY_LIMIT)
    log(
        f"QUEUE move History {source.name} before "
        f"{request.before_id or 'visible end'}"
    )


def _finish_waiting_mutation(
    buf: "queue.Queue[BufferedPiece]",
    st: State,
    current_name: str,
) -> int:
    try:
        return _reset_waiting_buffer(buf, st, current_name)
    except (OSError, RuntimeError) as error:
        raise MutationOutcomeUnconfirmed(
            "Waiting mutation committed but its audio buffer reset was unconfirmed"
        ) from error


def apply_archive_mutation(
    buf: "queue.Queue[BufferedPiece]",
    st: State,
    request: ArchiveMutation,
) -> None:
    """Move one Waiting row to History without changing Current."""
    ordered, source = timeline.waiting_source(request.id)
    current_name = ordered[0].name
    if not archive(source):
        raise RuntimeError(f"could not archive Waiting Speechicle: {request.id}")
    discarded = _finish_waiting_mutation(buf, st, current_name)
    log(f"QUEUE archive {source.name}; discarded {discarded} banked piece(s)")


def apply_waiting_move_mutation(
    buf: "queue.Queue[BufferedPiece]",
    st: State,
    request: MoveMutation,
) -> bool:
    """Move one Waiting row without changing Current."""
    current_path, source = timeline.reorder_waiting(request.id, request.before_id)
    if request.before_id == request.id:
        return False
    discarded = _finish_waiting_mutation(buf, st, current_path.name)
    log(
        f"QUEUE move {source.name} before {request.before_id or 'end'}; "
        f"discarded {discarded} banked piece(s)"
    )
    return True


def _publish_mutation_outcome(
    claimed: Path,
    st: State,
    request_id: str,
    outcome: str,
    *,
    result_id: str | None = None,
    error: str | None = None,
    lifecycle_state: LifecycleState | None = None,
) -> bool:
    """Publish the authoritative status before its matching mutation result."""
    if lifecycle_state is None and outcome == "unconfirmed" and st.stop.is_set():
        lifecycle_state = "stopped"
    snapshot = publish_status(
        st,
        lifecycle_state=lifecycle_state,
        force=True,
    )
    if snapshot is None:
        st.stop.set()
        log(f"could not publish status before mutation result {request_id}")
        return False
    published = publish_mutation_result(
        request_id,
        outcome,
        snapshot,
        result_id=result_id,
        error=error,
    )
    retire_claim(claimed, published)
    if not published:
        st.stop.set()
    return published


MutationEffect = Literal["queue_changed", "select", "clear"]


def process_mutation_requests(
    buf: "queue.Queue[BufferedPiece]",
    st: State,
    held_chunk_name: str | None = None,
) -> MutationEffect | None:
    """Apply claimable mutations in order and return their playback effect.

    A Play that restarts playback returns `select`; Clear returns `clear`. The
    later of those commands replaces the earlier result. With neither, a Queue
    change returns `queue_changed`; no relevant change returns `None`.
    """
    prune_mutation_results()
    effect: MutationEffect | None = None
    while not st.stop.is_set():
        try:
            claimed = claim_next_mutation_request()
        except MutationOutcomeUnconfirmed as error:
            st.stop.set()
            log(f"mutation claim outcome is unconfirmed: {error}")
            break
        if claimed is None:
            break
        request_id = request_id_from_claim_path(claimed)
        try:
            request = read_mutation_claim(claimed)
        except ValueError as error:
            log(f"invalid mutation request: {error}")
            if request_id is None:
                claimed.unlink(missing_ok=True)
                continue
            _publish_mutation_outcome(
                claimed,
                st,
                request_id,
                "rejected",
                error=str(error),
            )
            continue
        if request_id is None:
            log(f"invalid mutation filename: {claimed.name}")
            claimed.unlink(missing_ok=True)
            continue
        if request.request_id != request_id:
            _publish_mutation_outcome(
                claimed,
                st,
                request_id,
                "rejected",
                error="request ID does not match its mutation filename",
            )
            continue
        if not command_allowed_by_graceful_stop(request.command_sequence, st):
            _publish_mutation_outcome(
                claimed,
                st,
                request.request_id,
                "rejected",
                error="engine stopped before mutation was applied",
            )
            continue

        mutation_subject = (
            request.request_id
            if isinstance(request, (ClearMutation, EnqueueMutation))
            else request.id
        )
        result_id: str | None = None
        try:
            if isinstance(request, EnqueueMutation):
                queued = apply_enqueue_mutation(request)
                result_id = public_id_for_path(queued)
                effect = effect or "queue_changed"
            elif isinstance(request, PlayMutation):
                play_effect = apply_play_mutation(buf, st, request)
                effect = play_effect or effect
                result_id = request.id
            elif isinstance(request, ClearMutation):
                clear_committed = False
                playback_control.start_clearing(request.request_id)
                try:
                    publish_status(st, lifecycle_state="clearing", force=True)
                    if not do_clear(buf, st):
                        raise MutationOutcomeUnconfirmed(
                            "clear result was unconfirmed"
                        )
                    clear_committed = True
                finally:
                    playback_control.finish_clearing(
                        request.request_id,
                        hold_active=clear_committed
                    )
                effect = "clear"
            elif isinstance(request, MoveMutation):
                if request.section == "history":
                    apply_history_move_mutation(request)
                else:
                    queue_changed = apply_waiting_move_mutation(buf, st, request)
                    if queue_changed:
                        effect = effect or "queue_changed"
            elif isinstance(request, ArchiveMutation):
                apply_archive_mutation(buf, st, request)
                effect = effect or "queue_changed"
            elif isinstance(request, DeleteMutation):
                apply_delete_mutation(request)
            else:
                assert_never(request)
            finalize_accepted_command(
                request.command_sequence,
                st,
                resume=isinstance(request, (PlayMutation, ClearMutation)),
            )
        except MutationOutcomeUnconfirmed as error:
            st.stop.set()
            log(
                f"MUTATION {request.type} outcome is unconfirmed for "
                f"{mutation_subject}: {error}"
            )
            _publish_mutation_outcome(
                claimed,
                st,
                request.request_id,
                "unconfirmed",
                error=str(error),
            )
            break
        except (OSError, RuntimeError, ValueError) as error:
            log(f"MUTATION {request.type} rejected for {mutation_subject}: {error}")
            _publish_mutation_outcome(
                claimed,
                st,
                request.request_id,
                "rejected",
                error=str(error),
            )
        else:
            if not _publish_mutation_outcome(
                claimed,
                st,
                request.request_id,
                "committed",
                result_id=result_id,
            ):
                break
    if effect in {"select", "clear"} and held_chunk_name is not None:
        invalidate_claim(st, held_chunk_name)
    return effect


def reject_pending_requests(reason: str, st: State) -> None:
    """Give every published mutation a terminal result before the engine exits."""
    while not st.stop.is_set():
        try:
            claimed = claim_next_mutation_request()
        except MutationOutcomeUnconfirmed as error:
            st.stop.set()
            log(f"mutation claim outcome is unconfirmed: {error}")
            return
        if claimed is None:
            return
        request_id = request_id_from_claim_path(claimed)
        if request_id is None:
            claimed.unlink(missing_ok=True)
            continue
        try:
            request = read_mutation_claim(claimed)
        except ValueError as error:
            rejection = str(error)
        else:
            rejection = (
                reason
                if request.request_id == request_id
                else "request ID does not match its mutation filename"
            )
        if not _publish_mutation_outcome(
            claimed,
            st,
            request_id,
            "rejected",
            error=rejection,
            lifecycle_state="stopped",
        ):
            return


def _claimed(st: State, name: str, generation: int | None = None) -> bool:
    with st.lock:
        return (
            name in st.claims
            and (generation is None or st.claims[name] == generation)
        )


def invalidate_claim(st: State, name: str) -> None:
    """Make every buffered or in-flight piece from the current claim stale."""
    with st.lock:
        st.claims.pop(name, None)


def buffered_piece_is_stale(st: State, name: str, generation: int) -> bool:
    return not _claimed(st, name, generation)


def release_preplay_chunk(st: State, name: str, generation: int) -> None:
    """Release one exact worker claim before its first audio piece."""
    with st.lock:
        if st.claims.get(name) != generation:
            return
        st.claims.pop(name)
        clear_current_playback(st, name)


def _record_claim(st: State, path: Path) -> tuple[Path, int]:
    st.next_claim_generation += 1
    generation = st.next_claim_generation
    st.claims[path.name] = generation
    return path, generation


def claim_next_queued_chunk_with_generation(st: State) -> tuple[Path, int] | None:
    with st.lock:
        try:
            candidates = queue_files_in_order()
        except RuntimeError as error:
            log(str(error))
            return None
        if st.saw_stop:
            current_path = candidates[0] if candidates else None
            if current_path is not None and current_path.name not in st.claims:
                return _record_claim(st, current_path)
            return None
        for path in candidates:
            if path.name in st.claims:
                continue
            return _record_claim(st, path)
    return None


def synth_worker(kokoro, buf: "queue.Queue[BufferedPiece]", st: State) -> None:
    """Synthesize claimed Speechicles into a bounded queue of playback pieces."""
    while not st.stop.is_set():
        if consume(WARMUP):
            warmup(kokoro)
        claim = claim_next_queued_chunk_with_generation(st)
        if claim is None:
            heartbeat()
            time.sleep(POLL_INTERVAL)
            continue
        nxt, generation = claim
        try:
            text = nxt.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            log(f"read error {nxt.name}: {error}")
            with st.lock:
                if st.claims.get(nxt.name) != generation:
                    continue
                st.claims.pop(nxt.name)
                started = st.read_failures.setdefault(nxt.name, time.monotonic())
                deadline = 0.5 if st.saw_stop else 5.0
                if time.monotonic() - started >= deadline:
                    clear_current_playback(st, nxt.name)
                    st.stop.set()
                    log(f"stopping after a persistent read failure for {nxt.name}")
            time.sleep(0.05)
            continue
        with st.lock:
            st.read_failures.pop(nxt.name, None)
        if not text:
            log(f"empty {nxt.name}, archiving")
            if archive(nxt):
                release_preplay_chunk(st, nxt.name, generation)
            else:
                st.stop.set()
            continue
        voice = voice_from_name(nxt.name)
        pieces = split_text_pieces(text, SPLIT_CHARS)
        delivered_any = False
        for idx, piece in enumerate(pieces):
            terminal_failure = False
            if st.stop.is_set() or not _claimed(st, nxt.name, generation):
                break
            heartbeat(force=True)
            t0 = time.time()
            try:
                audio, sr = kokoro.create(
                    piece.text, voice=voice, speed=1.0, lang="en-us"
                )
            except Exception as e:
                if not _claimed(st, nxt.name, generation):
                    log(f"ignored stale synth error {nxt.name}[{idx}]: {e}")
                    break
                log(f"synth error {nxt.name}[{idx}] (voice={voice}): {e}")
                if not delivered_any:
                    with st.lock:
                        if (
                            st.claims.get(nxt.name) != generation
                            or st.stop.is_set()
                        ):
                            log(f"ignored stale synth error {nxt.name}[{idx}]")
                            break
                        if archive_failed(nxt):
                            release_preplay_chunk(st, nxt.name, generation)
                        else:
                            st.stop.set()
                    break
                # An empty last piece tells the player to finish after earlier audio
                audio = audio[:0]
                terminal_failure = True
            else:
                log(
                    f"synth {nxt.name}[{idx+1}/{len(pieces)}] voice={voice} "
                    f"chars={len(piece.text)} synth={time.time()-t0:.1f}s audio={len(audio)/sr:.1f}s"
                )
            entry = BufferedPiece(
                path=nxt,
                audio=audio,
                sample_rate=sr,
                is_first_piece=idx == 0,
                is_last_piece=terminal_failure or idx == len(pieces) - 1,
                piece_number=idx + 1,
                speechicle_text=text,
                piece_start=piece.start,
                piece_end=piece.end,
                claim_generation=generation,
            )
            while not st.stop.is_set():
                with st.lock:
                    if st.claims.get(nxt.name) != generation:
                        break
                    try:
                        buf.put_nowait(entry)
                    except queue.Full:
                        pass
                    else:
                        delivered_any = True
                        break
                heartbeat()
                time.sleep(SIGNAL_TICK)
            if terminal_failure:
                break


def gap_wait(
    seconds: float,
    buf: "queue.Queue[BufferedPiece]",
    st: State,
) -> str | None:
    """Wait for an audible gap without counting time spent paused."""
    try:
        ordered_queue = queue_files_in_order()
    except RuntimeError as error:
        log(str(error))
        st.stop.set()
        return "fatal"
    held_chunk_name = ordered_queue[0].name if ordered_queue else None
    remaining = seconds
    last_tick = time.monotonic()
    while remaining > 0:
        if st.stop.is_set():
            return "fatal"
        consume_continue(st)
        if consume_control(INTERRUPT):
            reject_pending_requests("engine interrupted before command was applied", st)
            return "interrupt"
        mutation_effect = process_mutation_requests(buf, st, held_chunk_name)
        if mutation_effect in {"select", "clear"}:
            return mutation_effect
        if mutation_effect == "queue_changed" and (
            held_chunk_name is None or not _claimed(st, held_chunk_name)
        ):
            return "queue_changed"
        if consume_ordered_marker(STOP):
            reject_pending_requests("engine stopped before command was applied", st)
            return "stop"
        if st.stop.is_set():
            return "fatal"
        if consume_control(SKIP):
            return "skip"
        now = time.monotonic()
        if playback_control.pause_requested():
            publish_status(st)
        else:
            remaining -= now - last_tick
            publish_status(st)
        last_tick = now
        time.sleep(SIGNAL_TICK)
    return None


_prev_audio_end: float | None = None


def play_one(
    sd,
    np,
    path: Path,
    audio,
    sr,
    kind: str,
    buf: "queue.Queue[BufferedPiece]",
    st: State,
) -> str:
    """Play one rendered piece while honoring pause and control signals."""
    global _prev_audio_end
    if st.stop.is_set():
        return "fatal"
    if len(audio) == 0:
        return "done"
    out = np.zeros(len(audio), dtype=getattr(audio, "dtype", "float32")) if SILENT else audio
    playback = PauseableAudio(out, sd.CallbackStop)
    paused = playback_control.attach(playback)
    t0 = time.time()
    stream = sd.OutputStream(
        samplerate=sr,
        channels=playback.channels,
        dtype=getattr(out, "dtype", "float32"),
        blocksize=max(64, int(sr * 0.02)),
        callback=playback.callback,
        finished_callback=playback.mark_done,
    )
    stream.start()
    publish_status(
        st,
        playback=playback,
        sample_rate=sr,
        force=True,
    )
    if _prev_audio_end is not None:
        log(f"boundary kind={kind} silence={(t0 - _prev_audio_end)*1000:.0f}ms before {path.name}")

    last_position = playback.position
    last_progress = time.monotonic()
    stalled = False
    try:
        while not playback.done.wait(SIGNAL_TICK):
            if st.stop.is_set():
                return "fatal"
            paused = playback_control.pause_requested()
            playback_state_changed = playback.set_paused(paused)
            if consume_control(INTERRUPT):
                reject_pending_requests(
                    "engine interrupted before command was applied", st
                )
                return "interrupt"
            consume_continue(st)
            mutation_effect = process_mutation_requests(buf, st)
            if mutation_effect in {"select", "clear"}:
                return mutation_effect
            if playback_state_changed:
                log(f"{'PAUSE' if paused else 'RESUME'} {path.name}")
                publish_status(
                    st,
                    playback=playback,
                    sample_rate=sr,
                    force=True,
                )
            if not st.saw_stop and ordered_marker_requested(STOP):
                st.saw_stop = True
            if st.saw_stop:
                reject_pending_requests("engine is stopping", st)
            if st.stop.is_set():
                return "fatal"
            if consume_control(SKIP):
                return "skip"

            position = playback.position
            now = time.monotonic()
            if playback.paused or position != last_position:
                last_position = position
                last_progress = now
            elif now - last_progress >= 2.0:
                stalled = True
                break

            publish_status(
                st,
                playback=playback,
                sample_rate=sr,
            )
            heartbeat()
    finally:
        if stream.active:
            stream.abort()
        stream.close()
        playback_control.detach(playback)

    if stalled:
        log(f"PLAYBACK_STALL {path.name}: no audio progress for 2s; cutting")

    wall = time.time() - t0
    dur = len(audio) / float(sr)
    _prev_audio_end = time.time()
    lag = wall - playback.paused_seconds - dur
    log(
        f"played {path.name} wall={wall:.1f}s audio={dur:.1f}s"
        + (f" UNDERRUN +{lag:.1f}s" if lag > 0.5 else "")
    )
    return "done"


def finish_chunk_playback(path: Path, outcome: str, last: bool, st: State) -> bool:
    """Settle a Speechicle after a piece finishes or playback stops early.

    Return `False` when normal playback should continue with another piece.
    Return `True` after every final, skipped, interrupted, or failed outcome.
    """
    if outcome == "done" and not last:
        return False
    released = outcome in {"select", "clear", "fatal"} or archive(path)
    with st.lock:
        if released:
            st.claims.pop(path.name, None)
        else:
            st.stop.set()
            log(
                f"could not archive {path.name}; stopping so it can be retried"
            )
        clear_current_playback(st, path.name)
    return True


def settle_stale_mutation_claims(st: State) -> None:
    """Report crash-interrupted claims without guessing whether they committed."""
    for claimed in sorted(BASE.glob(f"{MUTATION.stem}.*.claim")):
        request_id = request_id_from_claim_path(claimed)
        if request_id is None:
            claimed.unlink(missing_ok=True)
            continue
        if mutation_result_path(request_id).exists():
            claimed.unlink(missing_ok=True)
            continue
        _publish_mutation_outcome(
            claimed,
            st,
            request_id,
            "unconfirmed",
            error="engine restarted while mutation result was unconfirmed",
        )


def run_engine_loop(
    timeline_revision: int = 0,
    timeline_fingerprint_seed: str | None = None,
) -> None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    SPOKEN.mkdir(parents=True, exist_ok=True)
    st = State(timeline_revision, timeline_fingerprint_seed)
    publish_status(st, lifecycle_state="loading", force=True)
    settle_stale_mutation_claims(st)
    if not MODEL_PATH.exists() or not VOICES_PATH.exists():
        publish_status(st, lifecycle_state="setup_required", force=True)
        sys.stderr.write(f"missing kokoro files at {MODEL_DIR}\n")
        sys.exit(1)

    import numpy as np
    import sounddevice as sd
    # Try to reduce cold audio startup by opening one throwaway stream
    sd.play(np.zeros(int(0.1 * 24000), dtype=np.float32), 24000)
    sd.wait()
    log("loading kokoro model...")
    from kokoro_onnx import Kokoro
    # Cap ONNX below the detected CPU count; audio runs concurrently
    try:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, (os.cpu_count() or 4) - 2)
        kokoro = Kokoro.from_session(
            ort.InferenceSession(str(MODEL_PATH), sess_options=opts), str(VOICES_PATH)
        )
        log(f"synth capped at {opts.intra_op_num_threads} intra-op threads")
    except Exception as e:
        log(f"capped session failed ({e}); using default Kokoro init")
        kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    global AVAILABLE_VOICES
    try:
        AVAILABLE_VOICES = set(kokoro.get_voices())
    except Exception as e:
        log(f"could not enumerate voices: {e}; voice validation disabled")
        AVAILABLE_VOICES = set()
    log(
        f"kokoro loaded ({len(AVAILABLE_VOICES)} voices); buffered drainer "
        f"(buffer<={BUFFER_MAX}, gap={CHUNK_GAP_S}s, split={SPLIT_CHARS}c"
        f"{', SILENT' if SILENT else ''})"
    )

    warmup(kokoro)
    consume(WARMUP)

    buf: "queue.Queue[BufferedPiece]" = queue.Queue(maxsize=BUFFER_MAX)
    worker = threading.Thread(target=synth_worker, args=(kokoro, buf, st), daemon=True)
    worker.start()
    publish_status(st, force=True)

    session_first = True
    try:
        while True:
            if st.stop.is_set():
                log("engine stopping after a storage failure")
                return
            consume_continue(st)
            if consume_control(INTERRUPT):
                reject_pending_requests(
                    "engine interrupted before command was applied", st
                )
                log("INTERRUPT (idle); exiting")
                return
            process_mutation_requests(buf, st)
            if st.stop.is_set():
                log("engine stopping after a storage failure")
                return
            with st.lock:
                playing = st.current_projection.filename if st.current_projection is not None else None
            if not playing and (st.saw_stop or consume_ordered_marker(STOP)):
                reject_pending_requests(
                    "engine stopped before command was applied", st
                )
                log("STOP (idle); exiting")
                return
            if playing and not st.saw_stop and ordered_marker_requested(STOP):
                st.saw_stop = True
            if st.saw_stop:
                reject_pending_requests("engine is stopping", st)
            if st.stop.is_set():
                log("engine stopping after a storage failure")
                return
            if consume_control(SKIP):
                with st.lock:
                    current_name = st.current_projection.filename if st.current_projection is not None else None
                current_path = QUEUE / current_name if current_name else None
                if current_path is not None and current_path.exists():
                    finish_chunk_playback(current_path, "skip", True, st)
                    publish_status(st, force=True)
                    log(f"SKIP before playback {current_path.name}")
                    continue
                log("SKIP ignored (no Current)")

            try:
                buffered = buf.get(timeout=POLL_INTERVAL)
            except queue.Empty:
                heartbeat()
                publish_status(st)
                continue

            if st.stop.is_set():
                log("engine stopping before another buffered chunk starts")
                return

            # Drop pieces invalidated while the worker was handing them off
            if buffered_piece_is_stale(
                st,
                buffered.path.name,
                buffered.claim_generation,
            ):
                continue

            mutation_effect = process_mutation_requests(buf, st, buffered.path.name)
            if mutation_effect is not None and (
                mutation_effect != "queue_changed"
                or buffered_piece_is_stale(
                    st,
                    buffered.path.name,
                    buffered.claim_generation,
                )
            ):
                continue

            skip_initial_gap = buffered.is_first_piece and consume_initial_gap_skip(
                st,
                buffered.path.name,
            )
            if buffered.is_first_piece and not session_first and not skip_initial_gap:
                g = gap_from_name(buffered.path.name)
                outcome = gap_wait(g if g is not None else CHUNK_GAP_S, buf, st)
                if outcome == "interrupt":
                    log("INTERRUPT (gap); exiting")
                    return
                if outcome == "fatal":
                    log("engine stopping during the inter-chunk gap")
                    return
                if outcome == "select":
                    invalidate_claim(st, buffered.path.name)
                    continue
                if outcome == "stop":
                    invalidate_claim(st, buffered.path.name)
                    log("STOP (gap); exiting")
                    return
                if outcome in {"clear", "queue_changed"}:
                    continue
                if outcome == "skip":
                    finish_chunk_playback(buffered.path, "skip", True, st)
                    continue
            if st.stop.is_set():
                log("engine stopping before another chunk starts")
                return
            session_first = False

            if buffered.is_first_piece:
                if not start_current_playback(
                    st,
                    buffered.path.name,
                    buffered.speechicle_text,
                ):
                    invalidate_claim(st, buffered.path.name)
                    continue
            if not update_current_piece(
                st,
                buffered.path.name,
                buffered.piece_number,
                buffered.piece_start,
                buffered.piece_end,
            ):
                invalidate_claim(st, buffered.path.name)
                continue
            if buffered.is_first_piece:
                log(f"play {buffered.path.name}")
            publish_status(st, force=True)
            outcome = play_one(
                sd,
                np,
                buffered.path,
                buffered.audio,
                buffered.sample_rate,
                "speechicle" if buffered.is_first_piece else "piece",
                buf,
                st,
            )

            finished = finish_chunk_playback(
                buffered.path,
                outcome,
                buffered.is_last_piece,
                st,
            )
            if st.stop.is_set():
                log("engine stopping after a storage failure")
                return
            if finished:
                publish_status(st, force=True)

            if outcome == "interrupt":
                log("exiting on interrupt")
                return
            if st.saw_stop and (buffered.is_last_piece or outcome == "skip"):
                if consume_continue(st):
                    log("STOP canceled by newly queued speech")
                else:
                    consume_ordered_marker(STOP)
                    log("exiting on stop")
                    return

    except KeyboardInterrupt:
        log("KeyboardInterrupt; exiting")
    finally:
        st.stop.set()
        with st.lock:
            current_name = st.current_projection.filename if st.current_projection is not None else None
            clear_current_playback(st, current_name)
        publish_status(st, lifecycle_state="stopped", force=True)
        try:
            HEARTBEAT.unlink()
        except OSError:
            pass


def clear_transient_signals() -> None:
    with playback_command_lock():
        for signal in (STOP, CONTINUE):
            signal.unlink(missing_ok=True)
    for signal in (INTERRUPT, SKIP, WARMUP):
        signal.unlink(missing_ok=True)
    for temporary in BASE.glob(f"{MUTATION.stem}.*.tmp"):
        temporary.unlink(missing_ok=True)

    # Protocol 12 has one mutation stream, so no protocol 11 request may survive
    for pattern in (
        "CLEAR",
        "PLAY.json",
        "PLAY.*.json",
        "PLAY.*.tmp",
        "PLAY.*.claim",
        "PLAY_ACK.*.json",
        "QUEUE_COMMAND.json",
        "QUEUE_COMMAND.*.json",
        "QUEUE_COMMAND.*.tmp",
        "QUEUE_COMMAND.*.claim",
        "QUEUE_ACK.*.json",
    ):
        for artifact in BASE.glob(pattern):
            artifact.unlink(missing_ok=True)
    prune_mutation_results()


def serve() -> None:
    """Run the single engine process until a stop command or interruption."""
    global _playback_marker_executor
    instance_lock = EngineInstanceLock()
    if not instance_lock.acquire():
        return
    control_server: EngineControlServer | None = None
    playback_marker_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="super-speech-playback-marker",
    )
    _playback_marker_executor = playback_marker_executor
    try:
        STORAGE_READY.unlink(missing_ok=True)
        clear_transient_signals()
        timeline_revision, timeline_fingerprint_seed = load_timeline_revision_seed()
        publish_startup_status(timeline_revision)
        prepare_timeline_storage(instance_lock)
        control_server = EngineControlServer(
            BASE,
            os.getpid(),
            execute_control_request,
        )
        control_server.start()
        publish_storage_ready()
        run_engine_loop(timeline_revision, timeline_fingerprint_seed)
    finally:
        if control_server is not None:
            control_server.stop()
        playback_marker_executor.shutdown(wait=True)
        _playback_marker_executor = None
        STORAGE_READY.unlink(missing_ok=True)
        instance_lock.release()


def send_control(signal: Path) -> None:
    if not engine_is_running():
        raise RuntimeError("engine is not running")
    try:
        engine_pid = json.loads(STATUS.read_text(encoding="utf-8")).get("engine_pid")
    except (OSError, ValueError) as error:
        raise RuntimeError("engine status is unavailable") from error
    if not process_exists(engine_pid):
        raise RuntimeError("engine process is not running")
    if signal == STOP:
        publish_ordered_marker(signal, engine_pid=engine_pid)
        return
    temp_path = signal.with_name(f"{signal.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp_path.write_text(json.dumps({"engine_pid": engine_pid}), encoding="utf-8")
        os.replace(temp_path, signal)
    finally:
        temp_path.unlink(missing_ok=True)


def resume() -> None:
    remove_ordered_marker(PAUSE)


def _contains_private_status_field(value: object) -> bool:
    if isinstance(value, dict):
        return "filename" in value or any(
            _contains_private_status_field(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_private_status_field(item) for item in value)
    return False


def _stopped_status_payload() -> dict[str, object]:
    ordered_queue = queue_files_in_order()
    queue_items = []
    for path in ordered_queue:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        queue_items.append(_speechicle_status_item(path, text))
    current = None
    if queue_items:
        boundary = queue_items.pop(0)
        current_path = ordered_queue[0]
        projection = CurrentProjection(
            current_path.name,
            str(boundary["text"]),
        )
        current = _current_status_item(current_path, projection)
    history_count, history_items = history_snapshot()
    return _status_payload(
        timeline_revision=0,
        state="stopped",
        updated_at=0,
        engine_pid=None,
        current=current,
        queue_items=queue_items,
        history_count=history_count,
        history_items=history_items,
    )


def print_status() -> None:
    status_error: OSError | None = None
    for _ in range(5):
        try:
            stored_status = json.loads(STATUS.read_text(encoding="utf-8"))
        except OSError as error:
            status_error = error
            time.sleep(0.02)
        except json.JSONDecodeError:
            break
        else:
            status_error = None
            if (
                _snapshot_is_valid(stored_status)
                and not _contains_private_status_field(stored_status)
            ):
                assert isinstance(stored_status, dict)
                if (
                    engine_is_running()
                    and process_exists(stored_status.get("engine_pid"))
                ):
                    print(json.dumps(stored_status, ensure_ascii=False))
                    return
            break
    if STATUS.exists() and status_error is not None:
        raise RuntimeError(f"could not read engine status: {status_error}")
    instance_lock = EngineInstanceLock()
    if not instance_lock.acquire():
        if wait_for_engine_status():
            try:
                running_status = json.loads(STATUS.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise RuntimeError("engine status is unavailable") from error
            if _snapshot_is_valid(running_status):
                print(json.dumps(running_status, ensure_ascii=False))
                return
        raise RuntimeError("engine is starting and status is unavailable")
    try:
        prepare_timeline_storage(instance_lock)
        print(json.dumps(_stopped_status_payload(), ensure_ascii=False))
    finally:
        instance_lock.release()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="super-speech-engine")
    parser.add_argument("--version", action="version", version=ENGINE_VERSION)
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="command"
    )

    commands.add_parser("serve", help="run the speech engine")
    speak = commands.add_parser(
        "speak", help="start the engine and queue one Speechicle"
    )
    speak.add_argument("text", help="text to speak")
    speak.add_argument(
        "--voice",
        default="af_heart",
        type=_normalize_cli_voice,
        help="Kokoro voice ID",
    )
    speak.add_argument("--gap-ms", type=int, help="pre-speech gap from 0 to 1500 ms")
    speak.add_argument("--source", help="short agent or session label")
    speak.add_argument("--inbox", help="file that receives replies from the user")
    listen = commands.add_parser(
        "listen-inbox", help="print messages appended to an agent inbox"
    )
    listen.add_argument("inbox", help="inbox file to create and follow")
    listen.add_argument(
        "--from-end",
        action="store_true",
        help="ignore messages already present when listening starts",
    )
    setup = commands.add_parser("setup", help="download verified Kokoro models")
    setup.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    commands.add_parser("status", help="print the current runtime status")
    commands.add_parser("pause", help="pause at the current audio sample")
    commands.add_parser("resume", help="resume from the current audio sample")
    play = commands.add_parser(
        "play", help="play a Speechicle from Current, Waiting, or History by ID"
    )
    play.add_argument("speechicle_id", help="Speechicle ID from status output")
    play.add_argument(
        "--voice",
        type=_normalize_cli_voice,
        help="play the same text with another Kokoro voice",
    )
    move = commands.add_parser(
        "move", help="move a Waiting Speechicle before another ID"
    )
    move.add_argument("speechicle_id", help="Waiting Speechicle ID from status output")
    move.add_argument(
        "before_id",
        nargs="?",
        help="Waiting Speechicle ID to insert before; omit to move to the end",
    )
    move_history = commands.add_parser(
        "move-history", help="reorder one recent History Speechicle"
    )
    move_history.add_argument(
        "speechicle_id", help="History Speechicle ID from status output"
    )
    move_history.add_argument(
        "before_id",
        nargs="?",
        help="History Speechicle ID to insert before; omit to move it last on screen",
    )
    archive_command = commands.add_parser(
        "archive", help="move one Waiting Speechicle to History"
    )
    archive_command.add_argument(
        "speechicle_id", help="Waiting Speechicle ID from status output"
    )
    delete_command = commands.add_parser(
        "delete", help="permanently delete one History Speechicle"
    )
    delete_command.add_argument(
        "speechicle_id", help="History Speechicle ID from status output"
    )
    mutate = commands.add_parser("mutate")
    mutate.add_argument("mutation_json", help=argparse.SUPPRESS)
    commands.add_parser("skip", help="skip the current Speechicle")
    commands.add_parser("clear", help="move Current and Waiting speech to History")
    commands.add_parser("stop", help="finish the current Speechicle and stop")
    commands.add_parser("interrupt", help="stop playback and the engine immediately")

    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve()
        elif args.command == "speak":
            start_engine()
            queued = enqueue_text(
                args.text,
                args.voice,
                args.gap_ms,
                args.source,
                args.inbox,
            )
            publish_ordered_marker(CONTINUE)
            if not wait_for_queue_acceptance():
                sys.stderr.write(
                    "speech remains queued; playback will begin when the engine is ready\n"
                )
            print(public_id_for_path(queued))
        elif args.command == "listen-inbox":
            listen_inbox(args.inbox, from_end=args.from_end)
        elif args.command == "setup":
            install_models(args.model_dir)
        elif args.command == "status":
            print_status()
        elif args.command == "pause":
            BASE.mkdir(parents=True, exist_ok=True)
            publish_ordered_marker(PAUSE)
        elif args.command == "resume":
            resume()
        elif args.command in {
            "play",
            "move",
            "move-history",
            "archive",
            "delete",
            "clear",
        }:
            start_engine()
            if args.command == "play":
                request = build_mutation_request(
                    "play", id=args.speechicle_id, voice=args.voice
                )
            elif args.command in {"move", "move-history"}:
                request = build_mutation_request(
                    "move",
                    section="history" if args.command == "move-history" else "waiting",
                    id=args.speechicle_id,
                    before_id=args.before_id,
                )
            elif args.command == "clear":
                request = build_mutation_request("clear")
            else:
                request = build_mutation_request(
                    args.command,
                    id=args.speechicle_id,
                )
            print(
                json.dumps(
                    wait_for_mutation_result(request_mutation(request)),
                    ensure_ascii=False,
                )
            )
        elif args.command == "mutate":
            try:
                mutation_payload = json.loads(args.mutation_json)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid mutation JSON: {error}") from error
            start_engine()
            result = execute_mutation(mutation_payload)
            print(json.dumps(result, ensure_ascii=False))
        else:
            signal = {
                "skip": SKIP,
                "stop": STOP,
                "interrupt": INTERRUPT,
            }[args.command]
            send_control(signal)
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
