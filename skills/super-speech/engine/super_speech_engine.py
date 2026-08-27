#!/usr/bin/env python3
"""Local Super Speech engine and command-line interface.

A background synth WORKER thread synthesizes queued chunks continuously and
banks the rendered audio in a bounded buffer. The MAIN thread consumes that
buffer, playing each piece through the OS audio driver via sounddevice
(non-blocking) and staying responsive to control signals every ~20 ms.

Long chunks are split at sentence boundaries into pieces of at most
SPLIT_CHARS characters; each piece is synthesized and banked separately, so a
chunk starts playing once its FIRST piece renders (~1-2 s) instead of after
the whole chunk renders (10-20 s for a 700-char chunk). Pieces of one chunk
play back-to-back (the ~0.1-0.2 s of play-loop latency between them lands on
a sentence boundary, where a pause is natural); the configured inter-chunk
gap applies only before a chunk's first piece. This is what makes the
queue gap-proof even when a short chunk is followed by a much longer one —
the synth-ahead cushion only needs to cover one SENTENCE, not one chunk.

The CLI is the public control surface. It owns daemon startup, queue numbering,
and playback signals so desktop and headless installations use identical
behavior. Signal files in BASE are the engine's private process protocol:
  PAUSE      - pause immediately; keep the current sample position until removed
  STOP       - finish the current chunk, then exit cleanly
  INTERRUPT  - stop playback immediately and exit
  SKIP       - stop current chunk (all its remaining pieces), archive it,
               continue with next
  CLEAR      - drop the buffer and every non-playing queued chunk (-> spoken/);
               never truncates the currently playing chunk
  CONTINUE   - cancel a graceful stop because new speech was queued
  PLAY.*.json - select a queued or recent chunk by ID; selecting waiting speech
                archives the current item and older waiting items first
  QUEUE_COMMAND.*.json - reorder/archive Waiting or reorder/delete History by ID
  WARMUP     - synthesize a throwaway phrase to pay the first-inference cost

Env:
  SUPER_SPEECH_HOME        - override the runtime home directory
  SUPER_SPEECH_MODEL_DIR   - override the read-only Kokoro model directory
  SUPER_SPEECH_SILENT      - opt-in: play silence of identical duration instead
                             of audio (timing is preserved). For measuring gap
                             behavior without making sound; default off.
  SUPER_SPEECH_SPLIT_CHARS - sentence-piece target size; 0 disables splitting
"""
import argparse
import glob
import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import BinaryIO

from pauseable_audio import PauseableAudio

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_USER_HOME = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
BASE = Path(os.environ.get("SUPER_SPEECH_HOME") or (_USER_HOME / ".super-speech"))
QUEUE = BASE / "queue"
SPOKEN = BASE / "spoken"
FAILED = BASE / "failed"
LOG = BASE / "log.txt"

STOP = BASE / "STOP"
PAUSE = BASE / "PAUSE"
INTERRUPT = BASE / "INTERRUPT"
SKIP = BASE / "SKIP"
CLEAR = BASE / "CLEAR"
CONTINUE = BASE / "CONTINUE"
PLAY = BASE / "PLAY.json"
QUEUE_COMMAND = BASE / "QUEUE_COMMAND.json"
QUEUE_ORDER = BASE / "queue-order.json"
HISTORY_ORDER = BASE / "history-order.json"
WARMUP = BASE / "WARMUP"
HEARTBEAT = BASE / "engine.alive"
STATUS = BASE / "status.json"
INSTANCE_LOCK = BASE / "engine.lock"


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

ENGINE_VERSION = "0.4.9"
STATUS_VERSION = 9
QUEUE_ACTIONS = frozenset({"move", "move_history", "archive", "delete"})


class MutationOutcomeUnconfirmed(RuntimeError):
    """A durable mutation may have committed after its rollback failed."""


class EngineInstanceLock:
    """Hold the one-byte process lock that makes the engine single-instance."""

    def __init__(self) -> None:
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        BASE.mkdir(parents=True, exist_ok=True)
        lock_file = INSTANCE_LOCK.open("a+b")
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False
        self._file = lock_file
        return True

    def release(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


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
        try:
            stored_status = json.loads(STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            stored_status = {}
        version = stored_status.get("version")
        if version is not None and version != STATUS_VERSION:
            raise RuntimeError(
                f"running engine uses unsupported protocol version {version}; "
                "interrupt it and retry"
            )
        if wait_for_engine_status():
            return
        raise RuntimeError(f"engine lock is held but startup did not finish; inspect {LOG}")
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

    if wait_for_engine_status(process.pid, process):
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
) -> bool:
    """Wait until the lock owner publishes status after its startup cleanup."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if engine_is_running():
            try:
                status = json.loads(STATUS.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                status = {}
            updated_at = status.get("updated_at")
            pid_matches = expected_pid is None or status.get("engine_pid") == expected_pid
            owner_is_live = expected_pid is not None or process_exists(
                status.get("engine_pid")
            )
            if (
                status.get("version") == STATUS_VERSION
                and pid_matches
                and owner_is_live
                and isinstance(updated_at, (int, float))
            ):
                return True
        if process is not None and process.poll() is not None:
            if engine_is_running():
                expected_pid = None
                process = None
                continue
            break
        time.sleep(0.05)
    return False


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


def chunk_sequence(path: Path) -> int | None:
    """Return the leading queue sequence, including legacy suffixed IDs."""
    prefix = path.name.split("-", 1)[0]
    match = re.match(r"\d+", prefix)
    return int(match.group()) if match else None


def _next_chunk_number() -> int:
    QUEUE.mkdir(parents=True, exist_ok=True)
    SPOKEN.mkdir(parents=True, exist_ok=True)
    existing = [*QUEUE.glob("*.txt"), *SPOKEN.glob("*.txt")]
    numbers = [
        sequence
        for path in existing
        if (sequence := chunk_sequence(path)) is not None
    ]
    numbers.extend(
        int(path.stem) for path in QUEUE.glob("*.reserve") if path.stem.isdigit()
    )
    return max(numbers, default=0) + 1


def _reserve_queue_file(filename_tail: str, text: str) -> Path:
    number = _next_chunk_number()
    for candidate in range(number, number + 100):
        reservation = QUEUE / f"{candidate:03d}.reserve"
        try:
            reservation_descriptor = os.open(
                reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            continue
        os.close(reservation_descriptor)
        path = QUEUE / f"{candidate:03d}-{filename_tail}"
        temporary = QUEUE / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
            reservation.unlink(missing_ok=True)
    raise RuntimeError("could not reserve a speech queue number")


def enqueue_text(text: str, voice: str, gap_ms: int | None = None) -> Path:
    """Atomically append one chunk to the shared speech queue."""
    text = text.strip()
    if not text:
        raise ValueError("speech text cannot be empty")
    if not re.fullmatch(r"[ab][fm]_[a-z0-9_]+", voice):
        raise ValueError(f"invalid Kokoro voice: {voice}")
    if gap_ms is not None and not 0 <= gap_ms <= 1500:
        raise ValueError("gap must be between 0 and 1500 milliseconds")

    gap = f"-g{gap_ms}" if gap_ms is not None else ""
    return _reserve_queue_file(f"{voice}{gap}-say.txt", text)


def chunk_sort_key(path: Path) -> tuple[bool, int, str]:
    sequence = chunk_sequence(path)
    return (sequence is None, sequence or 0, path.name)


def history_sort_key(path: Path) -> tuple[bool, int, str]:
    sequence = chunk_sequence(path)
    return (sequence is not None, sequence or 0, path.name)


_queue_order_lock = threading.RLock()


def queue_files_in_order() -> list[Path]:
    """Return live queue files in explicit order, then append new arrivals."""
    with _queue_order_lock:
        live = {path.stem: path for path in QUEUE.glob("*.txt")}
        try:
            payload = json.loads(QUEUE_ORDER.read_text(encoding="utf-8"))
            saved_ids = (
                payload.get("ids", [])
                if isinstance(payload, dict) and payload.get("version") == 1
                else []
            )
            if not isinstance(saved_ids, list) or not all(
                isinstance(chunk_id, str) for chunk_id in saved_ids
            ):
                saved_ids = []
        except (OSError, ValueError, json.JSONDecodeError):
            saved_ids = []

        ordered = [live.pop(chunk_id) for chunk_id in saved_ids if chunk_id in live]
        ordered.extend(sorted(live.values(), key=chunk_sort_key))
        return ordered


def save_queue_order(paths: list[Path] | None = None) -> None:
    """Atomically persist the order of files that are still in the queue."""
    with _queue_order_lock:
        ordered = paths if paths is not None else queue_files_in_order()
        live_ids = {path.stem for path in QUEUE.glob("*.txt")}
        ids = [path.stem for path in ordered if path.stem in live_ids]
        missing = sorted(
            (path for path in QUEUE.glob("*.txt") if path.stem not in ids),
            key=chunk_sort_key,
        )
        ids.extend(path.stem for path in missing)
        if not ids:
            QUEUE_ORDER.unlink(missing_ok=True)
            return
        temp_path = QUEUE_ORDER.with_name(
            f"{QUEUE_ORDER.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temp_path.write_text(
                json.dumps({"version": 1, "ids": ids}), encoding="utf-8"
            )
            os.replace(temp_path, QUEUE_ORDER)
        finally:
            temp_path.unlink(missing_ok=True)


def log(msg: str) -> None:
    now = time.time()
    line = f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d} {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


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
    stem = name.rsplit(".", 1)[0]
    parts = stem.split("-", 2)
    if len(parts) < 2:
        return DEFAULT_VOICE
    raw = parts[1].lower()
    if not (raw.startswith(("a", "b")) and "_" in raw):
        return DEFAULT_VOICE
    if AVAILABLE_VOICES and raw not in AVAILABLE_VOICES:
        log(f"unknown voice {raw!r} in {name}; falling back to {DEFAULT_VOICE}")
        return DEFAULT_VOICE
    return raw


def gap_from_name(name: str) -> float | None:
    """Parse optional gap from filename: NNN-voice-gMMM-slug.txt -> MMM/1000 seconds.
    Returns None if no gap segment is present, in which case the default applies."""
    stem = name.rsplit(".", 1)[0]
    parts = stem.split("-")
    if len(parts) >= 3:
        token = parts[2]
        if token.startswith("g") and token[1:].isdigit():
            return int(token[1:]) / 1000.0
    return None


_SENT_RE = re.compile(r"(?<=[.!?…])\s+")


FIRST_PIECE_CHARS = 120  # small first piece: its synth is the only wait at a cold start


def split_text(text: str, target: int) -> list[str]:
    """Pack whole sentences into pieces of at most `target` chars (a single
    longer sentence stays intact). The first piece is capped tighter so a
    chunk starts playing fast even with no banked cushion. target<=0
    disables splitting."""
    if target <= 0:
        return [text]
    sents = [s for s in _SENT_RE.split(text) if s.strip()]
    pieces: list[str] = []
    cur = ""
    for s in sents:
        cap = min(FIRST_PIECE_CHARS, target) if not pieces else target
        if cur and len(cur) + 1 + len(s) > cap:
            pieces.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        pieces.append(cur)
    return pieces or [text]


def consume(signal: Path) -> bool:
    if not signal.exists():
        return False
    try:
        signal.unlink()
    except OSError:
        pass
    return True


def control_requested(signal: Path) -> bool:
    """Return whether a control belongs to this engine process.

    Empty files remain valid for compatibility with older clients. New clients
    include the lock owner's PID so a delayed command cannot affect its successor.
    """
    try:
        payload = signal.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not payload:
        return True
    try:
        target_pid = json.loads(payload).get("engine_pid")
    except (AttributeError, ValueError, json.JSONDecodeError):
        target_pid = None
    if target_pid == os.getpid():
        return True
    signal.unlink(missing_ok=True)
    log(f"ignored stale {signal.name} for engine {target_pid}")
    return False


def consume_control(signal: Path) -> bool:
    if not control_requested(signal):
        return False
    signal.unlink(missing_ok=True)
    return True


def play_ack_path(request_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", request_id):
        raise ValueError("invalid play request ID")
    return BASE / f"PLAY_ACK.{request_id}.json"


def request_play(chunk_id: str, voice: str | None = None) -> str:
    """Atomically publish one uniquely acknowledged selection request."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", chunk_id):
        raise ValueError("chunk ID contains invalid characters")
    if voice is not None and not re.fullmatch(r"[ab][fm]_[a-z0-9_]+", voice):
        raise ValueError("invalid Kokoro voice")
    if not engine_is_running():
        raise RuntimeError("engine is not running")

    BASE.mkdir(parents=True, exist_ok=True)
    request_id = secrets.token_hex(12)
    request_path = PLAY.with_name(f"{PLAY.stem}.{time.time_ns()}.{request_id}.json")
    temp_path = request_path.with_name(
        f"{request_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp_path.write_text(
            json.dumps({"id": chunk_id, "voice": voice, "request_id": request_id}),
            encoding="utf-8",
        )
        os.replace(temp_path, request_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return request_id


def claim_play_requests() -> list[Path]:
    """Atomically detach the pending request batch from concurrent writers."""
    claimed: list[Path] = []
    pattern = f"{PLAY.stem}.*.json"
    for request in sorted(BASE.glob(pattern)):
        claim = request.with_suffix(".claim")
        try:
            os.replace(request, claim)
        except FileNotFoundError:
            continue
        except OSError as error:
            log(f"could not claim play request {request.name}: {error}")
            continue
        claimed.append(claim)
    return claimed


def read_play_claim(claimed: Path) -> tuple[str, str | None, str] | None:
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        chunk_id = payload.get("id")
        voice = payload.get("voice")
        request_id = payload.get("request_id")
        if not isinstance(chunk_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", chunk_id
        ):
            raise ValueError("invalid chunk ID")
        if voice is not None and not (
            isinstance(voice, str) and re.fullmatch(r"[ab][fm]_[a-z0-9_]+", voice)
        ):
            raise ValueError("invalid Kokoro voice")
        if not isinstance(request_id, str):
            raise ValueError("invalid play request ID")
        play_ack_path(request_id)
        return chunk_id, voice, request_id
    except (OSError, ValueError, json.JSONDecodeError) as error:
        log(f"invalid play request: {error}")
        claimed.unlink(missing_ok=True)
        return None


def take_play_request() -> tuple[str, str | None, str, Path] | None:
    claimed = claim_play_requests()
    if not claimed:
        return None
    prune_play_acknowledgements()
    for superseded in claimed[:-1]:
        request = read_play_claim(superseded)
        if request is not None:
            _, _, request_id = request
            retire_claim(
                superseded,
                publish_play_ack(
                    request_id, error="superseded by a newer play request"
                ),
            )
    request = read_play_claim(claimed[-1])
    return (*request, claimed[-1]) if request is not None else None


def publish_play_ack(
    request_id: str,
    *,
    result_id: str | None = None,
    error: str | None = None,
    accepted_at: float | None = None,
) -> bool:
    target = play_ack_path(request_id)
    payload = {
        "ok": error is None,
        "result_id": result_id,
        "accepted_at": accepted_at if accepted_at is not None else time.time(),
        "error": error,
    }
    return publish_ack_payload(target, payload, "play")


def publish_ack_payload(
    target: Path, payload: dict[str, object], label: str
) -> bool:
    last_error: OSError | None = None
    for _ in range(5):
        temp_path = target.with_name(
            f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            temp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temp_path, target)
            return True
        except OSError as error:
            last_error = error
            time.sleep(0.02)
        finally:
            temp_path.unlink(missing_ok=True)
    log(f"could not publish {label} acknowledgement: {last_error}")
    return False


def retire_claim(claimed: Path, acknowledged: bool) -> None:
    """Remove an acknowledged claim; otherwise retain it for restart recovery."""
    if acknowledged:
        try:
            claimed.unlink(missing_ok=True)
        except OSError as error:
            log(f"could not remove acknowledged request {claimed.name}: {error}")
        return


def cancel_unclaimed_request(stem: str, request_id: str) -> bool:
    """Cancel a request only while the engine has not claimed it."""
    for _ in range(5):
        for request in BASE.glob(f"{stem}.*.{request_id}.json"):
            cancelled = request.with_suffix(".cancel")
            try:
                os.replace(request, cancelled)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            cancelled.unlink(missing_ok=True)
            return True
        time.sleep(0.02)
    return False


def request_is_unclaimed(stem: str, request_id: str) -> bool:
    return any(BASE.glob(f"{stem}.*.{request_id}.json"))


def request_is_claimed(stem: str, request_id: str) -> bool:
    return any(BASE.glob(f"{stem}.*.{request_id}.claim"))


def wait_for_request_ack(
    target: Path, stem: str, request_id: str, timeout: float
) -> dict[str, object] | None:
    payload = wait_for_ack_payload(target, time.monotonic() + timeout)
    if payload is not None:
        return payload
    unclaimed_deadline = time.monotonic() + min(max(timeout, 0.1), 5.0)
    while request_is_unclaimed(stem, request_id) and time.monotonic() < unclaimed_deadline:
        if cancel_unclaimed_request(stem, request_id):
            return None
        time.sleep(0.05)
    settlement_deadline = time.monotonic() + min(max(timeout, 0.1), 5.0)
    while request_is_claimed(stem, request_id) and time.monotonic() < settlement_deadline:
        if not engine_is_running():
            try:
                start_engine()
            except RuntimeError:
                break
        payload = wait_for_ack_payload(target, time.monotonic() + 1.0)
        if payload is not None:
            return payload
    return wait_for_ack_payload(target, time.monotonic() + 0.1)


def wait_for_ack_payload(target: Path, deadline: float) -> dict[str, object] | None:
    """Read one atomic acknowledgement, tolerating short Windows file locks."""
    while time.monotonic() < deadline:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except OSError:
            time.sleep(0.05)
            continue
        except json.JSONDecodeError as error:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"invalid engine acknowledgement: {error}") from error
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            log(f"could not remove acknowledgement {target.name}: {error}")
        if not isinstance(payload, dict):
            raise RuntimeError("invalid engine acknowledgement: expected an object")
        return payload
    return None


def wait_for_play_ack(request_id: str, timeout: float = 60.0) -> dict[str, object]:
    target = play_ack_path(request_id)
    payload = wait_for_request_ack(target, PLAY.stem, request_id, timeout)
    if payload is None:
        if request_is_claimed(PLAY.stem, request_id) or request_is_unclaimed(
            PLAY.stem, request_id
        ):
            raise RuntimeError("play command result was unconfirmed")
        raise RuntimeError("engine did not acknowledge play request")
    if payload.get("ok") is not True:
        raise RuntimeError(str(payload.get("error") or "engine rejected play request"))
    result_id = payload.get("result_id")
    accepted_at = payload.get("accepted_at")
    if not isinstance(result_id, str) or not isinstance(accepted_at, (int, float)):
        raise RuntimeError("engine returned an incomplete play acknowledgement")
    return {"id": result_id, "accepted_at": accepted_at}


def prune_play_acknowledgements(max_age: float = 300.0) -> None:
    cutoff = time.time() - max_age
    for acknowledgement in BASE.glob("PLAY_ACK.*.json"):
        try:
            if acknowledgement.stat().st_mtime < cutoff:
                acknowledgement.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            log(f"could not prune play acknowledgement {acknowledgement.name}: {error}")


def queue_ack_path(request_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", request_id):
        raise ValueError("invalid queue request ID")
    return BASE / f"QUEUE_ACK.{request_id}.json"


def request_queue_command(
    action: str, chunk_id: str, before_id: str | None = None
) -> str:
    """Publish one queue or History mutation for exact engine acknowledgement."""
    if action not in QUEUE_ACTIONS:
        raise ValueError("invalid queue action")
    for value in (chunk_id, before_id):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("chunk ID contains invalid characters")
    if action not in {"move", "move_history"} and before_id is not None:
        raise ValueError(f"{action} does not accept a destination")
    if not engine_is_running():
        raise RuntimeError("engine is not running")

    BASE.mkdir(parents=True, exist_ok=True)
    request_id = secrets.token_hex(12)
    request_path = QUEUE_COMMAND.with_name(
        f"{QUEUE_COMMAND.stem}.{time.time_ns()}.{request_id}.json"
    )
    temp_path = request_path.with_name(
        f"{request_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp_path.write_text(
            json.dumps(
                {
                    "action": action,
                    "id": chunk_id,
                    "before_id": before_id,
                    "request_id": request_id,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temp_path, request_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return request_id


def claim_queue_requests() -> list[Path]:
    claimed: list[Path] = []
    for request in sorted(BASE.glob(f"{QUEUE_COMMAND.stem}.*.json")):
        claim = request.with_suffix(".claim")
        try:
            os.replace(request, claim)
        except FileNotFoundError:
            continue
        except OSError as error:
            log(f"could not claim queue request {request.name}: {error}")
            continue
        claimed.append(claim)
    return claimed


def read_queue_claim(claimed: Path) -> tuple[str, str, str | None, str] | None:
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        action = payload.get("action")
        chunk_id = payload.get("id")
        before_id = payload.get("before_id")
        request_id = payload.get("request_id")
        if action not in QUEUE_ACTIONS:
            raise ValueError("invalid queue action")
        for value in (chunk_id, before_id):
            if value is not None and not (
                isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value)
            ):
                raise ValueError("invalid chunk ID")
        if not isinstance(chunk_id, str):
            raise ValueError("invalid chunk ID")
        if action not in {"move", "move_history"} and before_id is not None:
            raise ValueError(f"{action} does not accept a destination")
        if not isinstance(request_id, str):
            raise ValueError("invalid queue request ID")
        queue_ack_path(request_id)
        return action, chunk_id, before_id, request_id
    except (AttributeError, OSError, ValueError, json.JSONDecodeError) as error:
        log(f"invalid queue request: {error}")
        claimed.unlink(missing_ok=True)
        return None


def publish_queue_ack(request_id: str, error: str | None = None) -> bool:
    target = queue_ack_path(request_id)
    return publish_ack_payload(
        target,
        {
            "ok": error is None,
            "accepted_at": time.time(),
            "error": error,
        },
        "queue",
    )


def wait_for_queue_ack(request_id: str, timeout: float = 10.0) -> None:
    target = queue_ack_path(request_id)
    payload = wait_for_request_ack(target, QUEUE_COMMAND.stem, request_id, timeout)
    if payload is None:
        if request_is_claimed(
            QUEUE_COMMAND.stem, request_id
        ) or request_is_unclaimed(QUEUE_COMMAND.stem, request_id):
            raise RuntimeError("queue command result was unconfirmed")
        raise RuntimeError("engine did not acknowledge queue request")
    if payload.get("ok") is not True:
        raise RuntimeError(str(payload.get("error") or "engine rejected queue request"))
    if not isinstance(payload.get("accepted_at"), (int, float)):
        raise RuntimeError("engine returned an incomplete queue acknowledgement")


def prune_queue_acknowledgements(max_age: float = 300.0) -> None:
    cutoff = time.time() - max_age
    for acknowledgement in BASE.glob("QUEUE_ACK.*.json"):
        try:
            if acknowledgement.stat().st_mtime < cutoff:
                acknowledgement.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            log(f"could not prune queue acknowledgement {acknowledgement.name}: {error}")


def archive(path: Path) -> bool:
    with _history_order_lock:
        destination = SPOKEN / path.name
        history_item_exists = destination.exists()
        try:
            SPOKEN.mkdir(parents=True, exist_ok=True)
            os.replace(str(path), str(destination))
            try:
                save_queue_order()
            except OSError as order_error:
                # The live queue is authoritative if its optional order file cannot update
                log(f"queue order update error after archiving {path.name}: {order_error}")
            try:
                if history_item_exists:
                    save_history_order()
                else:
                    previous = [item for item in history_files_in_order() if item != destination]
                    save_history_order([destination, *previous])
            except OSError as order_error:
                log(f"history order update error after archiving {path.name}: {order_error}")
            invalidate_history()
            return True
        except FileNotFoundError as error:
            if not path.exists() and destination.exists():
                invalidate_history()
                return True
            log(f"archive error {path.name}: {error}")
        except OSError as error:
            log(f"archive error {path.name}: {error}")
    return False


def archive_failed(path: Path) -> bool:
    """Move a chunk that couldn't be synthesized into failed/ so the queue doesn't loop on it."""
    try:
        FAILED.mkdir(parents=True, exist_ok=True)
        os.replace(str(path), str(FAILED / path.name))
        return True
    except OSError as e:
        log(f"archive_failed error {path.name}: {e}")
        return False


def warmup(kokoro) -> None:
    """Synthesize a throwaway phrase and discard it to pay the one-time
    first-inference cost up front, so the first real chunk renders fast."""
    heartbeat(force=True)
    t0 = time.time()
    try:
        kokoro.create("Warming up the model.", voice=DEFAULT_VOICE, speed=1.0, lang="en-us")
        log(f"warmup (discarded) synth={time.time()-t0:.1f}s")
    except Exception as e:
        log(f"warmup error: {e}")


class State:
    """Shared coordination between the main (consumer) and worker (producer)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.claimed: set[str] = set()  # filenames in the buffer or being synthesized
        # Active playback-boundary item, including synthesis and inter-item gaps
        self.playing: str | None = None
        self.current_text: str | None = None
        self.current_voice: str | None = None
        self.current_piece = 0
        self.current_piece_count = 0
        self.recent_starts: list[tuple[str, float]] = []
        self.skip_name: str | None = None  # skipped chunk whose banked pieces must be dropped
        # Selected chunk stays prioritized until its first piece reaches playback
        self.selection_name: str | None = None
        self.stop = threading.Event()    # tell the worker to exit
        self.saw_stop = False            # latched STOP — finish current chunk, then exit


def record_started(st: State, chunk_id: str, started_at: float | None = None) -> float:
    """Retain enough first-piece receipts to survive slower renderer polling."""
    timestamp = started_at if started_at is not None else time.time()
    with st.lock:
        st.recent_starts = [
            (existing_id, existing_at)
            for existing_id, existing_at in st.recent_starts
            if existing_id != chunk_id
        ]
        st.recent_starts.insert(0, (chunk_id, timestamp))
        del st.recent_starts[20:]
    return timestamp


_last_status_write = 0.0
_history_dirty = True
_history_count = 0
_history_items: list[dict[str, object]] = []
_history_order_lock = threading.RLock()


def invalidate_history() -> None:
    global _history_dirty
    with _history_order_lock:
        _history_dirty = True


def history_files_in_order() -> list[Path]:
    """Return archived files in their saved display order."""
    with _history_order_lock:
        live = {path.stem: path for path in SPOKEN.glob("*.txt")}
        try:
            payload = json.loads(HISTORY_ORDER.read_text(encoding="utf-8"))
            saved_ids = (
                payload.get("ids", [])
                if isinstance(payload, dict) and payload.get("version") == 1
                else []
            )
            if not isinstance(saved_ids, list) or not all(
                isinstance(chunk_id, str) for chunk_id in saved_ids
            ):
                saved_ids = []
        except (OSError, ValueError, json.JSONDecodeError):
            saved_ids = []
        ordered = [live.pop(chunk_id) for chunk_id in saved_ids if chunk_id in live]
        ordered.extend(sorted(live.values(), key=history_sort_key, reverse=True))
        return ordered


def save_history_order(paths: list[Path] | None = None) -> None:
    """Atomically persist the order of archived files."""
    with _history_order_lock:
        ordered = paths if paths is not None else history_files_in_order()
        live_ids = {path.stem for path in SPOKEN.glob("*.txt")}
        ids = [path.stem for path in ordered if path.stem in live_ids]
        missing = sorted(
            (path for path in SPOKEN.glob("*.txt") if path.stem not in ids),
            key=history_sort_key,
            reverse=True,
        )
        ids.extend(path.stem for path in missing)
        if not ids:
            HISTORY_ORDER.unlink(missing_ok=True)
            return
        temp_path = HISTORY_ORDER.with_name(
            f"{HISTORY_ORDER.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temp_path.write_text(
                json.dumps({"version": 1, "ids": ids}), encoding="utf-8"
            )
            os.replace(temp_path, HISTORY_ORDER)
        finally:
            temp_path.unlink(missing_ok=True)


def history_snapshot() -> tuple[int, list[dict[str, object]]]:
    """Return the cached bounded archive view, refreshing only after an archive move."""
    global _history_dirty, _history_count, _history_items
    with _history_order_lock:
        if _history_dirty:
            history_files = history_files_in_order()
            items: list[dict[str, object]] = []
            for path in history_files[:HISTORY_LIMIT]:
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                items.append(
                    {
                        "id": path.stem,
                        "filename": path.name,
                        "text": text,
                        "voice": voice_from_name(path.name),
                    }
                )
            _history_count = len(history_files)
            _history_items = items
            _history_dirty = False
        return _history_count, _history_items


def activate_next_chunk(st: State) -> bool:
    """Give queued work one current item before publishing or synthesizing it."""
    with st.lock:
        if st.playing or st.saw_stop or st.stop.is_set():
            return False
        ordered = queue_files_in_order()
        if st.selection_name:
            selected = next(
                (path for path in ordered if path.name == st.selection_name),
                None,
            )
            if selected is not None:
                ordered.remove(selected)
                ordered.insert(0, selected)
            else:
                st.selection_name = None
        if not ordered:
            return False
        current = ordered[0]
        try:
            text = current.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        st.playing = current.name
        st.current_text = text
        st.current_voice = voice_from_name(current.name)
        st.current_piece = 0
        st.current_piece_count = len(split_text(text, SPLIT_CHARS))
        return True


def publish_status(
    playback_state: str,
    st: State,
    *,
    playback: PauseableAudio | None = None,
    sample_rate: int | None = None,
    force: bool = False,
) -> None:
    """Publish an atomic runtime snapshot for the desktop controller."""
    global _last_status_write
    now = time.time()
    if not force and now - _last_status_write < 0.25:
        return

    activate_next_chunk(st)
    with st.lock:
        playing = st.playing
        selected = st.selection_name
        current_text = st.current_text
        current_voice = st.current_voice
        current_piece = st.current_piece
        current_piece_count = st.current_piece_count
        recent_starts = list(st.recent_starts)

    ordered_queue = queue_files_in_order()
    current_path = next(
        (
            path
            for name in (selected, playing)
            if name
            for path in ordered_queue
            if path.name == name
        ),
        ordered_queue[0] if ordered_queue else None,
    )
    queue_files = [path for path in ordered_queue if path != current_path]
    queue_items = []
    for path in queue_files:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        queue_items.append(
            {
                "id": path.stem,
                "filename": path.name,
                "text": text,
                "voice": voice_from_name(path.name),
            }
        )

    current = None
    if current_path is not None:
        is_playing_path = current_path.name == playing
        if not is_playing_path:
            try:
                current_text = current_path.read_text(encoding="utf-8").strip()
            except OSError:
                current_text = ""
            current_voice = voice_from_name(current_path.name)
            current_piece = 0
            current_piece_count = len(split_text(current_text, SPLIT_CHARS))
        current = {
            "id": current_path.stem,
            "filename": current_path.name,
            "text": current_text or "",
            "voice": current_voice or voice_from_name(current_path.name),
            "piece": current_piece,
            "piece_count": current_piece_count,
            "elapsed_seconds": (
                playback.position / sample_rate
                if is_playing_path and playback is not None and sample_rate
                else 0.0
            ),
        }

    if playback_state not in {"loading", "setup_required", "stopped"}:
        has_work = current is not None
        playback_state = (
            "paused" if has_work and PAUSE.exists()
            else "playing" if has_work
            else "idle"
        )
    history_count, history_items = history_snapshot()

    payload = {
        "version": STATUS_VERSION,
        "state": playback_state,
        "updated_at": now,
        "engine_pid": os.getpid(),
        "current": current,
        "recent_starts": [
            {"id": chunk_id, "started_at": started_at}
            for chunk_id, started_at in recent_starts
        ],
        "queue_count": len(queue_items),
        "queue": queue_items,
        "history_count": history_count,
        "history": history_items,
    }
    temp_path = STATUS.with_name(f"{STATUS.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, STATUS)
        _last_status_write = now
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _find_chunk(directory: Path, chunk_id: str) -> Path | None:
    return next((path for path in directory.glob("*.txt") if path.stem == chunk_id), None)


def _voice_variant_path(source: Path, voice: str) -> Path:
    if AVAILABLE_VOICES and voice not in AVAILABLE_VOICES:
        raise ValueError(f"unknown Kokoro voice: {voice}")
    if not re.fullmatch(r"[ab][fm]_[a-z0-9_]+", voice):
        raise ValueError(f"invalid Kokoro voice: {voice}")
    match = re.fullmatch(
        r"(\d+)-[ab][fm]_[a-z0-9_]+((?:-g\d+)?-say)\.txt",
        source.name,
    )
    if match is None:
        raise ValueError(f"invalid speech filename: {source.name}")
    return source.with_name(f"{match.group(1)}-{voice}{match.group(2)}.txt")


def _replace_queue_voice(source: Path, voice: str) -> Path:
    """Rename one queued row in place so its chronology does not change."""
    target = _voice_variant_path(source, voice)
    if target == source:
        return source
    with _queue_order_lock:
        if target.exists():
            raise RuntimeError(f"voice target already exists: {target.stem}")
        ordered = queue_files_in_order()
        try:
            position = ordered.index(source)
        except ValueError as error:
            raise ValueError(f"waiting chunk not found: {source.stem}") from error
        os.replace(source, target)
        ordered[position] = target
        try:
            save_queue_order(ordered)
        except OSError as error:
            try:
                os.replace(target, source)
                save_queue_order(
                    ordered[:position] + [source] + ordered[position + 1 :]
                )
            except OSError as rollback_error:
                raise MutationOutcomeUnconfirmed(
                    f"voice change rollback failed: {rollback_error}"
                ) from error
            raise
    return target


def promote_history_selection(
    source: Path,
    voice: str | None = None,
) -> tuple[Path, int]:
    """Move the playback boundary to one History item without changing display order."""
    with _history_order_lock, _queue_order_lock:
        history = history_files_in_order()
        try:
            selected_index = history.index(source)
        except ValueError as error:
            raise ValueError(f"history chunk not found: {source.stem}") from error

        promoted = history[: selected_index + 1]
        remaining_history = history[selected_index + 1 :]
        previous_queue = queue_files_in_order()
        moved: list[tuple[Path, Path, Path | None]] = []
        renamed: tuple[Path, Path] | None = None
        try:
            for archived in promoted:
                queued = QUEUE / archived.name
                duplicate_backup = None
                if queued.exists():
                    duplicate_backup = archived.with_name(
                        f".{archived.name}.{os.getpid()}.{time.time_ns()}.duplicate"
                    )
                    os.replace(archived, duplicate_backup)
                else:
                    os.replace(archived, queued)
                moved.append((archived, queued, duplicate_backup))
            playback_order = [queued for _, queued, _ in reversed(moved)]
            promoted_paths = set(playback_order)
            remaining_queue = [
                path for path in previous_queue if path not in promoted_paths
            ]
            selected = playback_order[0]
            if voice and voice != voice_from_name(selected.name):
                variant = _voice_variant_path(selected, voice)
                if variant.exists():
                    raise RuntimeError(f"voice target already exists: {variant.stem}")
                os.replace(selected, variant)
                renamed = (selected, variant)
                playback_order[0] = variant
            save_queue_order([*playback_order, *remaining_queue])
            save_history_order(remaining_history)
            for _, _, duplicate_backup in moved:
                if duplicate_backup is not None:
                    try:
                        duplicate_backup.unlink(missing_ok=True)
                    except OSError as cleanup_error:
                        log(
                            f"could not remove legacy History duplicate "
                            f"{duplicate_backup.name}: {cleanup_error}"
                        )
        except (OSError, RuntimeError, ValueError) as error:
            recovery_errors = []
            if renamed is not None:
                original, variant = renamed
                try:
                    os.replace(variant, original)
                except OSError as recovery_error:
                    recovery_errors.append(str(recovery_error))
            for archived, queued, duplicate_backup in reversed(moved):
                try:
                    os.replace(
                        duplicate_backup if duplicate_backup is not None else queued,
                        archived,
                    )
                except OSError as recovery_error:
                    recovery_errors.append(str(recovery_error))
            try:
                save_queue_order(previous_queue)
                save_history_order(history)
            except OSError as recovery_error:
                recovery_errors.append(str(recovery_error))
            if recovery_errors:
                raise MutationOutcomeUnconfirmed(
                    "History boundary rollback failed: "
                    + "; ".join(recovery_errors)
                ) from error
            raise RuntimeError("could not move History playback boundary") from error
        finally:
            invalidate_history()
    return renamed[1] if renamed is not None else QUEUE / source.name, len(promoted)


def _copy_history_item(source: Path, target: Path) -> None:
    """Restore a queue file while retaining its preexisting History copy."""
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_bytes(source.read_bytes())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_archived_queue_items(
    moved: list[tuple[Path, bool]], ordered: list[Path]
) -> list[str]:
    """Restore a partially archived queue batch and return recovery errors."""
    errors = []
    with _history_order_lock:
        for original, keep_history in reversed(moved):
            history_item = SPOKEN / original.name
            try:
                if keep_history:
                    _copy_history_item(history_item, original)
                else:
                    os.replace(history_item, original)
            except OSError as error:
                errors.append(str(error))
        try:
            save_queue_order(ordered)
            save_history_order()
        except OSError as error:
            errors.append(str(error))
        invalidate_history()
    return errors


def _discard_buffer(buf: "queue.Queue") -> int:
    discarded = 0
    while True:
        try:
            buf.get_nowait()
            discarded += 1
        except queue.Empty:
            return discarded


def _reset_waiting_buffer(buf: "queue.Queue", st: State) -> int:
    """Drop rendered waiting audio while preserving every current-chunk piece."""
    with st.lock:
        kept = []
        discarded = 0
        while True:
            try:
                entry = buf.get_nowait()
            except queue.Empty:
                break
            if st.playing and entry[0].name == st.playing:
                kept.append(entry)
            else:
                discarded += 1
        for entry in kept:
            buf.put_nowait(entry)
        st.claimed = {entry[0].name for entry in kept}
        if st.playing:
            st.claimed.add(st.playing)
        if st.selection_name and _find_chunk(QUEUE, Path(st.selection_name).stem) is None:
            st.selection_name = None
        return discarded


def _archive_older_queue_items(target: Path, playing: str | None) -> list[Path]:
    """Archive the current chunk and each older waiting chunk before target."""
    ordered = queue_files_in_order()
    try:
        target_index = ordered.index(target)
    except ValueError as error:
        raise ValueError(f"waiting chunk not found: {target.stem}") from error

    archived = ordered[:target_index]
    if playing and playing != target.name:
        playing_path = next((path for path in ordered if path.name == playing), None)
        if playing_path is not None and playing_path not in archived:
            archived.append(playing_path)
    moved: list[tuple[Path, bool]] = []
    with _history_order_lock:
        for path in archived:
            history_existed = (SPOKEN / path.name).exists()
            if not archive(path):
                rollback_errors = _restore_archived_queue_items(moved, ordered)
                if rollback_errors:
                    raise MutationOutcomeUnconfirmed(
                        f"could not archive older waiting chunk {path.stem}; "
                        f"rollback failed: {'; '.join(rollback_errors)}"
                    )
                raise RuntimeError(f"could not archive older waiting chunk: {path.stem}")
            moved.append((path, history_existed))
    return archived


def process_play_request(buf: "queue.Queue", st: State) -> str | None:
    """Resolve one selected ID and return ``select`` when playback must yield."""
    request = take_play_request()
    if request is None:
        return None
    chunk_id, requested_voice, request_id, claim = request

    def acknowledge(
        *,
        result_id: str | None = None,
        error: str | None = None,
        accepted_at: float | None = None,
    ) -> None:
        retire_claim(
            claim,
            publish_play_ack(
                request_id,
                result_id=result_id,
                error=error,
                accepted_at=accepted_at,
            ),
        )

    # Discard audio rendered for the old order but keep source files queued for a clean restart
    with st.lock:
        playing = st.playing
    if (
        playing
        and Path(playing).stem == chunk_id
        and (requested_voice is None or requested_voice == voice_from_name(playing))
    ):
        STOP.unlink(missing_ok=True)
        PAUSE.unlink(missing_ok=True)
        with st.lock:
            st.saw_stop = False
        applied_at = record_started(st, chunk_id)
        publish_status("playing", st, force=True)
        acknowledge(result_id=chunk_id, accepted_at=applied_at)
        log(f"PLAY current {chunk_id}; resumed")
        return None

    target = _find_chunk(QUEUE, chunk_id)
    replayed_from: Path | None = None
    promoted_history_count = 0
    if target is None:
        replayed_from = _find_chunk(SPOKEN, chunk_id)
        if replayed_from is not None:
            try:
                target, promoted_history_count = promote_history_selection(
                    replayed_from,
                    requested_voice,
                )
            except MutationOutcomeUnconfirmed as error:
                log(f"History selection outcome is unconfirmed for {chunk_id}: {error}")
                publish_status("playing", st, force=True)
                acknowledge(error="play command result was unconfirmed")
                return None
            except (OSError, RuntimeError, ValueError) as error:
                log(f"could not replay {chunk_id}: {error}")
                acknowledge(error=f"could not replay {chunk_id}")
                return None
    if target is None:
        log(f"PLAY ignored; chunk not found: {chunk_id}")
        acknowledge(error=f"chunk not found: {chunk_id}")
        return None

    voice_original: Path | None = None
    if (
        replayed_from is None
        and requested_voice
        and requested_voice != voice_from_name(target.name)
    ):
        original = target
        voice_original = original
        try:
            target = _replace_queue_voice(original, requested_voice)
        except MutationOutcomeUnconfirmed as error:
            log(f"voice change outcome is unconfirmed for {chunk_id}: {error}")
            publish_status("playing", st, force=True)
            acknowledge(error="play command result was unconfirmed")
            return None
        except (OSError, RuntimeError, ValueError) as error:
            log(f"could not change voice for {chunk_id}: {error}")
            acknowledge(error=f"could not change voice for {chunk_id}")
            return None

    archived: list[Path] = []
    if replayed_from is None:
        try:
            archived = _archive_older_queue_items(target, playing)
        except (RuntimeError, ValueError) as error:
            outcome_unconfirmed = isinstance(error, MutationOutcomeUnconfirmed)
            if voice_original is not None:
                rollback_errors = []
                try:
                    _replace_queue_voice(target, voice_from_name(voice_original.name))
                except (OSError, RuntimeError, ValueError) as rollback_error:
                    rollback_errors.append(str(rollback_error))
                if rollback_errors:
                    log(f"voice selection rollback error: {'; '.join(rollback_errors)}")
                    outcome_unconfirmed = True
            if outcome_unconfirmed:
                log(f"selection outcome is unconfirmed for {chunk_id}: {error}")
                publish_status("playing", st, force=True)
                acknowledge(error="play command result was unconfirmed")
                return None
            log(f"could not select {chunk_id}: {error}")
            acknowledge(error=f"could not select {chunk_id}")
            return None

    STOP.unlink(missing_ok=True)
    PAUSE.unlink(missing_ok=True)
    with st.lock:
        discarded = _discard_buffer(buf)
        st.claimed.clear()
        st.selection_name = target.name
        st.skip_name = None
        st.saw_stop = False
    publish_status("playing", st, force=True)
    acknowledge(result_id=target.stem)
    if replayed_from is None:
        log(
            f"PLAY selected {target.name}; archived {len(archived)} older chunk(s); "
            f"discarded {discarded} banked piece(s)"
        )
    else:
        log(
            f"PLAY History {replayed_from.name} as {target.name}; "
            f"promoted {promoted_history_count} item(s); "
            f"discarded {discarded} banked piece(s)"
        )
    return "select"


def drop_to_spoken(path: Path) -> bool:
    return archive(path) or archive_failed(path)


def do_clear(buf: "queue.Queue", st: State) -> None:
    """Drop the rendered buffer and every non-playing queued chunk into spoken/.
    Banked pieces of the currently playing chunk are kept — CLEAR never
    truncates mid-chunk."""
    with st.lock:
        keep = []
        waiting: dict[str, Path] = {}
        blocked: set[str] = set()
        n = 0
        while True:
            try:
                entry = buf.get_nowait()
            except queue.Empty:
                break
            if st.playing and entry[0].name == st.playing:
                keep.append(entry)
            else:
                waiting[entry[0].name] = entry[0]
        for entry in keep:
            buf.put_nowait(entry)
        for f in glob.glob(str(QUEUE / "*.txt")):
            if os.path.basename(f) == st.playing:
                continue
            path = Path(f)
            waiting[path.name] = path
        for path in waiting.values():
            if drop_to_spoken(path):
                n += 1
            else:
                blocked.add(path.name)
        st.claimed = blocked | ({st.playing} if st.playing else set())
        st.selection_name = None
        if blocked:
            st.stop.set()
            log(
                "CLEAR could not archive "
                f"{len(blocked)} chunk(s); stopping so they can be retried"
            )
    log(f"CLEAR; dropped {n} buffered/queued chunk(s)")


def apply_queue_command(
    buf: "queue.Queue",
    st: State,
    action: str,
    chunk_id: str,
    before_id: str | None,
) -> None:
    """Apply one queue or History mutation without changing current playback."""
    if action == "delete":
        with st.lock:
            playing_id = Path(st.playing).stem if st.playing else None
        if _find_chunk(QUEUE, chunk_id) is not None or playing_id == chunk_id:
            raise ValueError(f"history chunk is active: {chunk_id}")
        with _history_order_lock:
            history_item = _find_chunk(SPOKEN, chunk_id)
            if history_item is None:
                log(f"QUEUE delete {chunk_id}; already absent")
                return
            try:
                history_item.unlink()
            except OSError as error:
                raise RuntimeError(f"could not delete history chunk: {chunk_id}") from error
            try:
                save_history_order()
            except OSError as order_error:
                log(
                    f"history order update error after deleting {history_item.name}: "
                    f"{order_error}"
                )
        invalidate_history()
        log(f"QUEUE delete {history_item.name}")
        return

    if action == "move_history":
        with _history_order_lock:
            ordered_history = history_files_in_order()
            source = next((path for path in ordered_history if path.stem == chunk_id), None)
            if source is None:
                raise ValueError(f"history chunk not found: {chunk_id}")
            if before_id == chunk_id:
                return
            ordered_history.remove(source)
            if before_id is None:
                ordered_history.insert(min(HISTORY_LIMIT - 1, len(ordered_history)), source)
            else:
                destination = next(
                    (path for path in ordered_history if path.stem == before_id),
                    None,
                )
                if destination is None:
                    raise ValueError(f"history destination not found: {before_id}")
                ordered_history.insert(ordered_history.index(destination), source)
            save_history_order(ordered_history)
        invalidate_history()
        log(f"QUEUE move History {source.name} before {before_id or 'visible end'}")
        return

    ordered = queue_files_in_order()
    source = next((path for path in ordered if path.stem == chunk_id), None)
    with st.lock:
        playing = st.playing
        selected = st.selection_name
    if source is None or source.name == playing:
        raise ValueError(f"waiting chunk not found: {chunk_id}")
    if source.name == selected:
        raise ValueError(f"selected speech is starting: {chunk_id}")

    if action == "archive":
        if not archive(source):
            raise RuntimeError(f"could not archive waiting chunk: {chunk_id}")
        discarded = _reset_waiting_buffer(buf, st)
        log(f"QUEUE archive {source.name}; discarded {discarded} banked piece(s)")
        return

    if action != "move":
        raise ValueError("invalid queue action")
    if before_id == chunk_id:
        return
    ordered.remove(source)
    if before_id is None:
        ordered.append(source)
    else:
        destination = next(
            (path for path in ordered if path.stem == before_id and path.name != playing),
            None,
        )
        if destination is None:
            raise ValueError(f"waiting destination not found: {before_id}")
        ordered.insert(ordered.index(destination), source)
    save_queue_order(ordered)
    with st.lock:
        if st.selection_name == source.name:
            st.selection_name = None
    discarded = _reset_waiting_buffer(buf, st)
    log(
        f"QUEUE move {source.name} before {before_id or 'end'}; "
        f"discarded {discarded} banked piece(s)"
    )


def process_queue_requests(buf: "queue.Queue", st: State) -> bool:
    """Apply every queued mutation in publication order and acknowledge each caller."""
    claimed_requests = claim_queue_requests()
    if not claimed_requests:
        return False
    prune_queue_acknowledgements()
    changed = False
    for claimed in claimed_requests:
        request = read_queue_claim(claimed)
        if request is None:
            continue
        action, chunk_id, before_id, request_id = request
        try:
            apply_queue_command(buf, st, action, chunk_id, before_id)
        except (OSError, RuntimeError, ValueError) as error:
            log(f"QUEUE {action} rejected for {chunk_id}: {error}")
            acknowledged = publish_queue_ack(request_id, error=str(error))
        else:
            changed = True
            acknowledged = publish_queue_ack(request_id)
        retire_claim(claimed, acknowledged)
    return changed


def reject_pending_requests(reason: str) -> None:
    """Give every caller a terminal answer before this engine exits."""
    for claimed in claim_play_requests():
        request = read_play_claim(claimed)
        if request is not None:
            _, _, request_id = request
            retire_claim(
                claimed,
                publish_play_ack(request_id, error=reason),
            )
    for claimed in claim_queue_requests():
        request = read_queue_claim(claimed)
        if request is not None:
            _, _, _, request_id = request
            retire_claim(
                claimed,
                publish_queue_ack(request_id, error=reason),
            )


def _claimed(st: State, name: str) -> bool:
    with st.lock:
        return (name in st.claimed or name == st.playing) and st.skip_name != name


def release_preplay_chunk(st: State, name: str) -> None:
    """Remove a worker-owned item that ended before its first audio piece."""
    with st.lock:
        st.claimed.discard(name)
        if st.selection_name == name:
            st.selection_name = None
        if st.playing == name:
            clear_current_playback(st)


def claim_next_queued_chunk(st: State) -> Path | None:
    with st.lock:
        if st.saw_stop:
            return None
        candidates = queue_files_in_order()
        if st.selection_name:
            selected = next(
                (path for path in candidates if path.name == st.selection_name),
                None,
            )
            if selected is not None:
                candidates.remove(selected)
                candidates.insert(0, selected)
                if selected.name not in st.claimed:
                    st.claimed.add(selected.name)
                    return selected
            else:
                st.selection_name = None
        if st.playing:
            active = next(
                (path for path in candidates if path.name == st.playing),
                None,
            )
            if active is not None and active.name not in st.claimed:
                st.claimed.add(active.name)
                return active
        for path in candidates:
            if path.name in st.claimed:
                continue
            st.claimed.add(path.name)
            return path
    return None


def synth_worker(kokoro, buf: "queue.Queue", st: State) -> None:
    """Producer: claim the next queued chunk, split it into sentence pieces,
    synthesize each, and bank playback entries in buf (blocking when the buffer
    is full - natural backpressure). Abandons a chunk's
    remaining pieces when CLEAR/SKIP unclaims it."""
    while not st.stop.is_set():
        if consume(WARMUP):
            warmup(kokoro)
        nxt = claim_next_queued_chunk(st)
        if nxt is None:
            heartbeat()
            time.sleep(POLL_INTERVAL)
            continue
        try:
            text = nxt.read_text(encoding="utf-8").strip()
        except Exception as e:
            log(f"read error {nxt.name}: {e}")
            release_preplay_chunk(st, nxt.name)
            continue
        if not text:
            log(f"empty {nxt.name}, archiving")
            if archive(nxt):
                release_preplay_chunk(st, nxt.name)
            else:
                st.stop.set()
            continue
        voice = voice_from_name(nxt.name)
        pieces = split_text(text, SPLIT_CHARS)
        delivered = 0
        for idx, piece in enumerate(pieces):
            if st.stop.is_set() or not _claimed(st, nxt.name):
                break
            heartbeat(force=True)
            t0 = time.time()
            try:
                audio, sr = kokoro.create(piece, voice=voice, speed=1.0, lang="en-us")
            except Exception as e:
                log(f"synth error {nxt.name}[{idx}] (voice={voice}): {e}")
                if delivered == 0:
                    if archive_failed(nxt):
                        release_preplay_chunk(st, nxt.name)
                    else:
                        st.stop.set()
                    break
                # Mid-chunk failure: deliver an empty terminal piece so the
                # consumer still archives and releases the chunk.
                audio = audio[:0]
            else:
                log(
                    f"synth {nxt.name}[{idx+1}/{len(pieces)}] voice={voice} "
                    f"chars={len(piece)} synth={time.time()-t0:.1f}s audio={len(audio)/sr:.1f}s"
                )
            entry = (
                nxt,
                audio,
                sr,
                idx == 0,
                idx == len(pieces) - 1,
                idx + 1,
                len(pieces),
                text,
                voice,
            )
            while not st.stop.is_set():
                with st.lock:
                    if (
                        (nxt.name not in st.claimed and nxt.name != st.playing)
                        or nxt.name == st.skip_name
                    ):
                        break
                    try:
                        buf.put_nowait(entry)
                    except queue.Full:
                        pass
                    else:
                        delivered += 1
                        break
                heartbeat()
                time.sleep(SIGNAL_TICK)


def gap_wait(seconds: float, buf: "queue.Queue", st: State) -> str | None:
    """Wait for an audible gap without counting time spent paused."""
    remaining = seconds
    last_tick = time.monotonic()
    while remaining > 0:
        if st.stop.is_set():
            return "fatal"
        if consume(CONTINUE):
            STOP.unlink(missing_ok=True)
            st.saw_stop = False
        if consume_control(INTERRUPT):
            reject_pending_requests("engine interrupted before command was applied")
            return "interrupt"
        if consume_control(STOP):
            reject_pending_requests("engine stopped before command was applied")
            return "stop"
        if process_play_request(buf, st) == "select":
            return "select"
        if process_queue_requests(buf, st):
            return "queue_changed"
        if consume_control(SKIP):
            return "skip"
        if consume_control(CLEAR):
            do_clear(buf, st)
            return "clear"
        now = time.monotonic()
        if PAUSE.exists():
            publish_status("paused", st)
        else:
            remaining -= now - last_tick
            publish_status("idle", st)
        last_tick = now
        time.sleep(SIGNAL_TICK)
    return None


_prev_audio_end: float | None = None


def play_one(sd, np, path: Path, audio, sr, kind: str, buf: "queue.Queue", st: State) -> str:
    """Play one rendered piece while honoring pause and control signals."""
    global _prev_audio_end
    if st.stop.is_set():
        return "fatal"
    if len(audio) == 0:
        return "done"
    out = np.zeros(len(audio), dtype=getattr(audio, "dtype", "float32")) if SILENT else audio
    playback = PauseableAudio(out, sd.CallbackStop)
    playback.set_paused(PAUSE.exists())
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
    record_started(st, path.stem)
    publish_status(
        "paused" if PAUSE.exists() else "playing",
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
                stream.abort()
                return "fatal"
            paused = PAUSE.exists()
            if playback.set_paused(paused):
                log(f"{'PAUSE' if paused else 'RESUME'} {path.name}")
                publish_status(
                    "paused" if paused else "playing",
                    st,
                    playback=playback,
                    sample_rate=sr,
                    force=True,
                )
            if consume_control(INTERRUPT):
                reject_pending_requests("engine interrupted before command was applied")
                stream.abort()
                return "interrupt"
            if consume(CONTINUE):
                STOP.unlink(missing_ok=True)
                st.saw_stop = False
            if not st.saw_stop and control_requested(STOP):
                st.saw_stop = True
            if st.saw_stop:
                reject_pending_requests("engine is stopping")
            else:
                if process_play_request(buf, st) == "select":
                    stream.abort()
                    return "select"
                process_queue_requests(buf, st)
            if consume_control(SKIP):
                stream.abort()
                return "skip"
            if consume_control(CLEAR):
                do_clear(buf, st)

            position = playback.position
            now = time.monotonic()
            if playback.paused or position != last_position:
                last_position = position
                last_progress = now
            elif now - last_progress >= 2.0:
                stalled = True
                break

            publish_status(
                "paused" if playback.paused else "playing",
                st,
                playback=playback,
                sample_rate=sr,
            )
            heartbeat()
    finally:
        if stream.active:
            stream.abort()
        stream.close()

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
    """Release a completed or interrupted chunk and report whether state changed."""
    if outcome == "done" and not last:
        return False
    released = outcome in {"select", "fatal"} or archive(path)
    if not released:
        released = archive_failed(path)
    with st.lock:
        if released:
            st.claimed.discard(path.name)
        else:
            st.stop.set()
            log(
                f"could not archive {path.name}; stopping so it can be retried"
            )
        clear_current_playback(st)
        if outcome == "skip":
            st.skip_name = path.name
    return True


def clear_current_playback(st: State) -> None:
    """Clear the transient current-item projection while holding st.lock."""
    st.playing = None
    st.current_text = None
    st.current_voice = None
    st.current_piece = 0
    st.current_piece_count = 0


def run_engine_loop() -> None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    SPOKEN.mkdir(parents=True, exist_ok=True)
    st = State()
    publish_status("loading", st, force=True)
    if not MODEL_PATH.exists() or not VOICES_PATH.exists():
        publish_status("setup_required", st, force=True)
        sys.stderr.write(f"missing kokoro files at {MODEL_DIR}\n")
        sys.exit(1)

    import numpy as np
    import sounddevice as sd
    # Warm up PortAudio so the first real sd.play() doesn't pay device-open latency.
    sd.play(np.zeros(int(0.1 * 24000), dtype=np.float32), 24000)
    sd.wait()
    log("loading kokoro model...")
    from kokoro_onnx import Kokoro
    # Cap ONNX intra-op threads below the core count: leaves headroom for the
    # audio callback under synth bursts AND measures faster than the
    # oversubscribed default (3.9x vs 3.1x realtime on this 8-core box).
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

    # Pay the one-time first-inference cost now (before the worker starts, so
    # there is no concurrent kokoro call), then clear any pre-launch WARMUP.
    warmup(kokoro)
    consume(WARMUP)

    buf: "queue.Queue" = queue.Queue(maxsize=BUFFER_MAX)
    worker = threading.Thread(target=synth_worker, args=(kokoro, buf, st), daemon=True)
    worker.start()
    publish_status("idle", st, force=True)

    session_first = True
    try:
        while True:
            if st.stop.is_set():
                log("engine stopping after a storage failure")
                return
            if consume(CONTINUE):
                STOP.unlink(missing_ok=True)
                st.saw_stop = False
            if consume_control(INTERRUPT):
                reject_pending_requests("engine interrupted before command was applied")
                log("INTERRUPT (idle); exiting")
                return
            with st.lock:
                playing = st.playing
            if not playing and (st.saw_stop or consume_control(STOP)):
                reject_pending_requests("engine stopped before command was applied")
                log("STOP (idle); exiting")
                return
            if playing and not st.saw_stop and control_requested(STOP):
                st.saw_stop = True
            if st.saw_stop:
                reject_pending_requests("engine is stopping")
            else:
                process_play_request(buf, st)
                process_queue_requests(buf, st)
            if consume_control(CLEAR):
                do_clear(buf, st)
            if consume_control(SKIP):
                log("SKIP ignored (idle)")

            try:
                (
                    path,
                    audio,
                    sr,
                    first,
                    last,
                    piece,
                    piece_count,
                    text,
                    voice,
                ) = buf.get(timeout=POLL_INTERVAL)
            except queue.Empty:
                heartbeat()
                publish_status("idle", st)
                continue

            if st.stop.is_set():
                log("engine stopping before another buffered chunk starts")
                return

            # Drop pieces invalidated while the worker was handing them off
            with st.lock:
                stale = st.skip_name == path.name or path.name not in st.claimed
                selected = not stale and first and st.selection_name == path.name
                if first and st.skip_name and not stale:
                    st.skip_name = None
            if stale:
                continue

            if process_queue_requests(buf, st):
                with st.lock:
                    st.claimed.discard(path.name)
                continue

            if first and not session_first and not selected:
                g = gap_from_name(path.name)
                outcome = gap_wait(g if g is not None else CHUNK_GAP_S, buf, st)
                if outcome == "interrupt":
                    log("INTERRUPT (gap); exiting")
                    return
                if outcome == "fatal":
                    log("engine stopping during the inter-chunk gap")
                    return
                if outcome == "select":
                    with st.lock:
                        st.claimed.discard(path.name)
                    continue
                if outcome == "stop":
                    with st.lock:
                        st.claimed.discard(path.name)
                    log("STOP (gap); exiting")
                    return
                if outcome == "clear":
                    continue
                if outcome == "queue_changed":
                    with st.lock:
                        st.claimed.discard(path.name)
                    continue
                if outcome == "skip":
                    finish_chunk_playback(path, "skip", True, st)
                    continue
            if st.stop.is_set():
                log("engine stopping before another chunk starts")
                return
            session_first = False

            if first:
                with st.lock:
                    st.playing = path.name
                    st.current_text = text
                    st.current_voice = voice
                    if selected:
                        st.selection_name = None
            with st.lock:
                st.current_piece = piece
                st.current_piece_count = piece_count
            if first:
                log(f"play {path.name}")
            publish_status("playing", st, force=True)
            outcome = play_one(sd, np, path, audio, sr,
                               "chunk" if first else "piece", buf, st)

            finished = finish_chunk_playback(path, outcome, last, st)
            if st.stop.is_set():
                log("engine stopping after a storage failure")
                return
            if finished:
                publish_status("idle", st, force=True)

            if outcome == "interrupt":
                log("exiting on interrupt")
                return
            if st.saw_stop and (last or outcome == "skip"):
                if consume(CONTINUE):
                    STOP.unlink(missing_ok=True)
                    st.saw_stop = False
                    log("STOP canceled by newly queued speech")
                else:
                    consume_control(STOP)
                    log("exiting on stop")
                    return

    except KeyboardInterrupt:
        log("KeyboardInterrupt; exiting")
    finally:
        st.stop.set()
        with st.lock:
            clear_current_playback(st)
        publish_status("stopped", st, force=True)
        try:
            HEARTBEAT.unlink()
        except OSError:
            pass


def clear_transient_signals() -> None:
    for signal in (
        STOP,
        INTERRUPT,
        SKIP,
        CLEAR,
        CONTINUE,
        PLAY,
        QUEUE_COMMAND,
        WARMUP,
    ):
        signal.unlink(missing_ok=True)
    for temporary in BASE.glob(f"{PLAY.stem}.*.tmp"):
        temporary.unlink(missing_ok=True)
    for temporary in BASE.glob(f"{QUEUE_COMMAND.stem}.*.tmp"):
        temporary.unlink(missing_ok=True)
    for claimed in BASE.glob(f"{PLAY.stem}.*.claim"):
        request = read_play_claim(claimed)
        if request is not None:
            _, _, request_id = request
            if play_ack_path(request_id).exists():
                claimed.unlink(missing_ok=True)
                continue
            retire_claim(
                claimed,
                publish_play_ack(
                    request_id, error="engine restarted while command result was unconfirmed"
                ),
            )
    for claimed in BASE.glob(f"{QUEUE_COMMAND.stem}.*.claim"):
        request = read_queue_claim(claimed)
        if request is not None:
            _, _, _, request_id = request
            if queue_ack_path(request_id).exists():
                claimed.unlink(missing_ok=True)
                continue
            retire_claim(
                claimed,
                publish_queue_ack(
                    request_id, error="engine restarted while command result was unconfirmed"
                ),
            )
    prune_play_acknowledgements()
    prune_queue_acknowledgements()


def serve() -> None:
    """Run the single engine process until a stop command or interruption."""
    instance_lock = EngineInstanceLock()
    if not instance_lock.acquire():
        return
    try:
        STATUS.unlink(missing_ok=True)
        clear_transient_signals()
        run_engine_loop()
    finally:
        instance_lock.release()


def send_control(signal: Path) -> None:
    if not engine_is_running():
        raise RuntimeError("engine is not running")
    try:
        engine_pid = json.loads(STATUS.read_text(encoding="utf-8")).get("engine_pid")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("engine status is unavailable") from error
    if not process_exists(engine_pid):
        raise RuntimeError("engine process is not running")
    temp_path = signal.with_name(f"{signal.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp_path.write_text(json.dumps({"engine_pid": engine_pid}), encoding="utf-8")
        os.replace(temp_path, signal)
    finally:
        temp_path.unlink(missing_ok=True)


def resume() -> None:
    PAUSE.unlink(missing_ok=True)


def print_status() -> None:
    status_error: OSError | None = None
    for _ in range(5):
        try:
            print(STATUS.read_text(encoding="utf-8"))
            return
        except OSError as error:
            status_error = error
            time.sleep(0.02)
    if STATUS.exists() and status_error is not None:
        raise RuntimeError(f"could not read engine status: {status_error}")
    queue_items = []
    for path in queue_files_in_order():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        queue_items.append(
            {
                "id": path.stem,
                "filename": path.name,
                "text": text,
                "voice": voice_from_name(path.name),
            }
        )
    current = None
    if queue_items:
        boundary = queue_items.pop(0)
        current = {
            **boundary,
            "piece": 0,
            "piece_count": len(split_text(str(boundary["text"]), SPLIT_CHARS)),
            "elapsed_seconds": 0.0,
        }
    print(
        json.dumps(
            {
                "version": STATUS_VERSION,
                "state": "stopped",
                "updated_at": 0,
                "engine_pid": None,
                "current": current,
                "recent_starts": [],
                "queue_count": len(queue_items),
                "queue": queue_items,
                "history_count": len(list(SPOKEN.glob("*.txt"))),
                "history": [],
            }
        )
    )


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="super-speech-engine")
    parser.add_argument("--version", action="version", version=ENGINE_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="run the speech engine")
    speak = commands.add_parser("speak", help="start the engine and queue one chunk")
    speak.add_argument("text", help="text to speak")
    speak.add_argument("--voice", default="af_heart", help="Kokoro voice ID")
    speak.add_argument("--gap-ms", type=int, help="pre-speech gap from 0 to 1500 ms")
    setup = commands.add_parser("setup", help="download verified Kokoro models")
    setup.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    commands.add_parser("status", help="print the current runtime status")
    commands.add_parser("pause", help="pause at the current audio sample")
    commands.add_parser("resume", help="resume from the current audio sample")
    play = commands.add_parser("play", help="play a queued or recent chunk by ID")
    play.add_argument("chunk_id", help="chunk ID from status output")
    play.add_argument("--voice", help="play the same text with another Kokoro voice")
    move = commands.add_parser("move", help="move a waiting chunk before another ID")
    move.add_argument("chunk_id", help="waiting chunk ID from status output")
    move.add_argument(
        "before_id",
        nargs="?",
        help="waiting chunk ID to insert before; omit to move to the end",
    )
    move_history = commands.add_parser(
        "move-history", help="reorder one recent History chunk"
    )
    move_history.add_argument("chunk_id", help="History chunk ID from status output")
    move_history.add_argument(
        "before_id",
        nargs="?",
        help="History chunk ID to insert before; omit to move it last on screen",
    )
    archive_command = commands.add_parser(
        "archive", help="move one waiting chunk to History"
    )
    archive_command.add_argument("chunk_id", help="waiting chunk ID from status output")
    delete_command = commands.add_parser(
        "delete", help="permanently delete one History chunk"
    )
    delete_command.add_argument("chunk_id", help="History chunk ID from status output")
    commands.add_parser("skip", help="skip the current chunk")
    commands.add_parser("clear", help="clear queued chunks after the current chunk")
    commands.add_parser("stop", help="finish the current chunk and stop")
    commands.add_parser("interrupt", help="stop playback and the engine immediately")

    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve()
        elif args.command == "speak":
            start_engine()
            queued = enqueue_text(args.text, args.voice, args.gap_ms)
            CONTINUE.touch()
            if not wait_for_queue_acceptance():
                sys.stderr.write(
                    "speech remains queued; playback will begin when the engine is ready\n"
                )
            print(queued)
        elif args.command == "setup":
            install_models(args.model_dir)
        elif args.command == "status":
            print_status()
        elif args.command == "pause":
            BASE.mkdir(parents=True, exist_ok=True)
            PAUSE.touch()
        elif args.command == "resume":
            resume()
        elif args.command == "play":
            start_engine()
            request_id = request_play(args.chunk_id, args.voice)
            print(json.dumps(wait_for_play_ack(request_id)))
        elif args.command in {"move", "move-history", "archive", "delete"}:
            start_engine()
            action = "move_history" if args.command == "move-history" else args.command
            request_id = request_queue_command(
                action,
                args.chunk_id,
                args.before_id if action in {"move", "move_history"} else None,
            )
            wait_for_queue_ack(request_id)
        else:
            signal = {
                "skip": SKIP,
                "clear": CLEAR,
                "stop": STOP,
                "interrupt": INTERRUPT,
            }[args.command]
            send_control(signal)
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
