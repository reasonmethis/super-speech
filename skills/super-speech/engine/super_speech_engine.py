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
  CONTINUE   - cancel a graceful stop because new speech was queued
  MUTATION.*.json - play, move, archive, delete, or clear the timeline in one
                    durable FIFO stream
  WARMUP     - synthesize a throwaway phrase to pay the first-inference cost

Env:
  SUPER_SPEECH_HOME        - override the runtime home directory
  SUPER_SPEECH_MODEL_DIR   - override the read-only Kokoro model directory
  SUPER_SPEECH_SILENT      - opt-in: play silence of identical duration instead
                             of audio (timing is preserved). For measuring gap
                             behavior without making sound; default off.
  SUPER_SPEECH_SPLIT_CHARS - internal synthesis-piece target size; 0 disables splitting
"""
import argparse
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
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, NamedTuple, assert_never

from mutation_protocol import (
    ArchiveMutation,
    ClearMutation,
    DeleteMutation,
    MoveMutation,
    MutationRequest,
    PlayMutation,
    parse_cli_mutation,
    parse_durable_mutation,
    validate_request_id,
)
from pauseable_audio import PauseableAudio
from speechicle_identity import (
    IdentityCatalog,
    allocate_identity,
    is_public_id,
    load_catalog,
    migration_from_intent,
    plan_identity_migration,
    sequence_for_public_id,
    strict_sequence,
    write_catalog,
)

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
CONTINUE = BASE / "CONTINUE"
MUTATION = BASE / "MUTATION.json"
QUEUE_ORDER = BASE / "queue-order.json"
HISTORY_ORDER = BASE / "history-order.json"
WARMUP = BASE / "WARMUP"
HEARTBEAT = BASE / "engine.alive"
STATUS = BASE / "status.json"
STATUS_FAILURE = BASE / "status.failed"
INSTANCE_LOCK = BASE / "engine.lock"
TIMELINE_LOCK = BASE / "timeline.lock"
TIMELINE_INTENT = BASE / "timeline-intent.json"
IDENTITY_INDEX = BASE / "speechicle-index.json"


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

ENGINE_VERSION = "0.4.11"
STATUS_VERSION = 12


class MutationOutcomeUnconfirmed(RuntimeError):
    """A durable mutation may have committed after its rollback failed."""


class InterprocessFileLock:
    """Hold one cross-platform, one-byte advisory file lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        BASE.mkdir(parents=True, exist_ok=True)
        lock_file = self._path.open("a+b")
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


class EngineInstanceLock(InterprocessFileLock):
    """Hold the process lock that makes the engine single-instance."""

    def __init__(self) -> None:
        super().__init__(INSTANCE_LOCK)


_timeline_lock_local = threading.local()


@contextmanager
def timeline_mutation_lock(timeout: float = 10.0):
    """Serialize cross-process operations that move or allocate timeline rows."""
    depth = getattr(_timeline_lock_local, "depth", 0)
    if depth:
        _timeline_lock_local.depth = depth + 1
        try:
            yield
        finally:
            _timeline_lock_local.depth -= 1
        return
    lock = InterprocessFileLock(TIMELINE_LOCK)
    deadline = time.monotonic() + timeout
    try:
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for the speech timeline lock")
            time.sleep(0.01)
        _timeline_lock_local.depth = 1
        yield
    finally:
        _timeline_lock_local.depth = 0
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


def _reserve_queue_file_unlocked(filename_tail: str, text: str) -> Path:
    _ensure_identity_catalog_unlocked()
    for _ in range(100):
        catalog = _load_identity_catalog(refresh=True)
        catalog, candidate, _ = allocate_identity(catalog)
        # Retire the sequence before creating its file. A failed write may leave a
        # gap, but a later Speechicle can never inherit the abandoned identity
        _write_identity_catalog(catalog)
        path = QUEUE / f"{candidate:03d}-{filename_tail}"
        if any((directory / path.name).exists() for directory in (QUEUE, SPOKEN, FAILED)):
            continue
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
    raise RuntimeError("could not reserve a speech queue number")


def _reserve_queue_file(filename_tail: str, text: str) -> Path:
    with timeline_mutation_lock():
        return _reserve_queue_file_unlocked(filename_tail, text)


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
_order_cache: dict[Path, list[str]] = {}
_identity_cache_lock = threading.RLock()
_identity_cache_path: Path | None = None
_identity_cache: IdentityCatalog | None = None
_identity_ready_path: Path | None = None


def _write_order_payload(path: Path, ids: list[str], version: int) -> None:
    if not ids:
        path.unlink(missing_ok=True)
        _order_cache[path] = []
        return
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps({"version": version, "ids": ids}), encoding="utf-8"
        )
        os.replace(temporary, path)
        _order_cache[path] = ids
    finally:
        temporary.unlink(missing_ok=True)


def _load_identity_catalog(*, refresh: bool = False) -> IdentityCatalog:
    global _identity_cache_path, _identity_cache
    with _identity_cache_lock:
        if refresh or _identity_cache_path != IDENTITY_INDEX or _identity_cache is None:
            try:
                catalog = load_catalog(IDENTITY_INDEX)
            except (TypeError, ValueError) as error:
                raise RuntimeError(str(error)) from error
            if catalog is None:
                raise RuntimeError("speech identity catalog is missing")
            _identity_cache_path = IDENTITY_INDEX
            _identity_cache = catalog
        return _identity_cache


def _write_identity_catalog(catalog: IdentityCatalog) -> None:
    global _identity_cache_path, _identity_cache
    write_catalog(IDENTITY_INDEX, catalog)
    with _identity_cache_lock:
        _identity_cache_path = IDENTITY_INDEX
        _identity_cache = catalog


def public_id_for_path(path: Path) -> str:
    sequence = strict_sequence(path.name)
    if sequence is None:
        raise RuntimeError(f"speech file has no storage sequence: {path.name}")
    catalog = _load_identity_catalog()
    public_id = catalog.public_id(sequence)
    if public_id is None:
        catalog = _load_identity_catalog(refresh=True)
        public_id = catalog.public_id(sequence)
    if public_id is None:
        raise RuntimeError(f"speech file has no public identity: {path.name}")
    return public_id


def _path_for_public_id(directory: Path, public_id: str) -> Path | None:
    sequence = sequence_for_public_id(_load_identity_catalog(), public_id)
    if sequence is None:
        return None
    return next(
        (path for path in directory.glob("*.txt") if strict_sequence(path.name) == sequence),
        None,
    )


def _apply_identity_migration_intent(intent: dict[str, object]) -> None:
    try:
        migration = migration_from_intent(intent)
    except (TypeError, ValueError) as error:
        raise RuntimeError("invalid pending identity migration") from error
    directories = {"queue": QUEUE, "spoken": SPOKEN, "failed": FAILED}
    for removal in migration.removals:
        (directories[removal.directory] / removal.name).unlink(missing_ok=True)
    for move in migration.moves:
        source = directories[move.source_directory] / move.source
        target = directories[move.target_directory] / move.target
        if source == target or (target.exists() and not source.exists()):
            continue
        if source.exists() and not target.exists():
            os.replace(source, target)
            continue
        raise RuntimeError(f"identity migration could not reconcile {source.name}")
    _write_identity_catalog(migration.catalog)
    _write_order_payload(QUEUE_ORDER, list(migration.queue_ids), 2)
    _write_order_payload(HISTORY_ORDER, list(migration.history_ids), 2)


def _ensure_identity_catalog_unlocked() -> None:
    global _identity_ready_path
    if _identity_ready_path == IDENTITY_INDEX and IDENTITY_INDEX.exists():
        return
    try:
        existing = load_catalog(IDENTITY_INDEX)
        migration = plan_identity_migration(
            QUEUE,
            SPOKEN,
            FAILED,
            QUEUE_ORDER,
            HISTORY_ORDER,
            existing,
            DEFAULT_VOICE,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError(str(error)) from error
    if migration is not None:
        intent = migration.intent_payload()
        _write_timeline_intent(intent)
        _apply_identity_migration_intent(intent)
        TIMELINE_INTENT.unlink()
        log(f"migrated {migration.row_count} speech identity row(s)", stderr=True)
    else:
        if existing is None:
            raise RuntimeError("speech identity catalog migration did not create a catalog")
        _write_identity_catalog(existing)
    _identity_ready_path = IDENTITY_INDEX


def ensure_identity_catalog() -> None:
    if _identity_ready_path == IDENTITY_INDEX and IDENTITY_INDEX.exists():
        return
    with timeline_mutation_lock(), _history_order_lock, _queue_order_lock:
        _recover_timeline_intent()
        _ensure_identity_catalog_unlocked()


def _read_saved_order(path: Path) -> list[str]:
    ensure_identity_catalog()
    for attempt in range(3):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            ids = (
                payload.get("ids", [])
                if isinstance(payload, dict) and payload.get("version") == 2
                else []
            )
            result = (
                ids
                if isinstance(ids, list) and all(isinstance(item, str) for item in ids)
                else []
            )
            _order_cache[path] = result
            return result
        except FileNotFoundError:
            _order_cache[path] = []
            return []
        except OSError:
            if path in _order_cache:
                return list(_order_cache[path])
            if attempt < 2:
                time.sleep(0.01)
                continue
            raise RuntimeError(f"could not read speech order: {path.name}")
        except (ValueError, json.JSONDecodeError):
            _order_cache[path] = []
            return []
    raise AssertionError("unreachable")


def _write_saved_order(path: Path, ids: list[str]) -> None:
    _write_order_payload(path, ids, 2)


def _write_timeline_intent(payload: dict[str, object]) -> None:
    if TIMELINE_INTENT.exists():
        raise RuntimeError("a timeline transaction is already pending recovery")
    temporary = TIMELINE_INTENT.with_name(
        f"{TIMELINE_INTENT.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, TIMELINE_INTENT)
    finally:
        temporary.unlink(missing_ok=True)


def queue_files_in_order() -> list[Path]:
    """Return live queue files in explicit order, then append new arrivals."""
    ensure_identity_catalog()
    with _queue_order_lock:
        live = {public_id_for_path(path): path for path in QUEUE.glob("*.txt")}
        ordered = []
        for speechicle_id in _read_saved_order(QUEUE_ORDER):
            matched = live.pop(speechicle_id, None)
            if matched is not None:
                ordered.append(matched)
        ordered.extend(sorted(live.values(), key=chunk_sort_key))
        return ordered


def save_queue_order(paths: list[Path] | None = None) -> None:
    """Atomically persist the order of files that are still in the queue."""
    ensure_identity_catalog()
    with _queue_order_lock:
        ordered = paths if paths is not None else queue_files_in_order()
        live_ids = {public_id_for_path(path) for path in QUEUE.glob("*.txt")}
        ids = [
            public_id_for_path(path)
            for path in ordered
            if public_id_for_path(path) in live_ids
        ]
        missing = sorted(
            (
                path
                for path in QUEUE.glob("*.txt")
                if public_id_for_path(path) not in ids
            ),
            key=chunk_sort_key,
        )
        ids.extend(public_id_for_path(path) for path in missing)
        _write_saved_order(QUEUE_ORDER, ids)


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


def mutation_result_path(request_id: str) -> Path:
    validate_request_id(request_id)
    return BASE / f"MUTATION_RESULT.{request_id}.json"


def request_mutation(request: MutationRequest) -> str:
    """Publish one validated mutation to the engine's durable FIFO stream."""
    if not engine_is_running():
        raise RuntimeError("engine is not running")

    BASE.mkdir(parents=True, exist_ok=True)
    request_path = MUTATION.with_name(
        f"{MUTATION.stem}.{time.time_ns()}.{request.request_id}.json"
    )
    temp_path = request_path.with_name(
        f"{request_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp_path.write_text(
            json.dumps(request.to_payload(), separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp_path, request_path)
    finally:
        temp_path.unlink(missing_ok=True)
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
            os.replace(request, claim)
        except FileNotFoundError:
            continue
        except OSError as error:
            log(f"could not claim mutation {request.name}: {error}")
            continue
        return claim
    return None


def read_mutation_claim(claimed: Path) -> MutationRequest:
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read mutation: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid mutation JSON: {error}") from error
    return parse_durable_mutation(payload)


def request_id_from_claim_path(claimed: Path) -> str | None:
    parts = claimed.name.split(".")
    if len(parts) != 4 or parts[0] != MUTATION.stem or parts[-1] != "claim":
        return None
    try:
        return validate_request_id(parts[-2])
    except ValueError:
        return None


def publish_json_payload(
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
    log(f"could not publish {label}: {last_error}")
    return False


def publish_mutation_result(
    request_id: str,
    outcome: str,
    snapshot: dict[str, object],
    *,
    result_id: str | None = None,
    error: str | None = None,
) -> bool:
    if outcome not in {"committed", "rejected", "unconfirmed"}:
        raise ValueError("invalid mutation outcome")
    if not _snapshot_is_valid(snapshot):
        raise ValueError("invalid mutation snapshot")
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
    return publish_json_payload(
        mutation_result_path(request_id),
        payload,
        "mutation result",
    )


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
                os.replace(request, cancelled)
            except FileNotFoundError:
                continue
            except OSError:
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
    target: Path, request_id: str, timeout: float
) -> dict[str, object] | None:
    payload = wait_for_json_payload(target, time.monotonic() + timeout)
    if payload is not None:
        return payload
    unclaimed_deadline = time.monotonic() + min(max(timeout, 0.1), 5.0)
    while mutation_is_unclaimed(request_id) and time.monotonic() < unclaimed_deadline:
        if cancel_unclaimed_mutation(request_id):
            return None
        time.sleep(0.05)
    settlement_deadline = time.monotonic() + min(max(timeout, 0.1), 5.0)
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


def _snapshot_is_valid(snapshot: object) -> bool:
    return (
        isinstance(snapshot, dict)
        and snapshot.get("version") == STATUS_VERSION
        and isinstance(snapshot.get("timeline_revision"), int)
        and snapshot.get("state")
        in {"idle", "loading", "paused", "playing", "setup_required", "stopped"}
        and isinstance(snapshot.get("queue"), list)
        and isinstance(snapshot.get("history"), list)
    )


def wait_for_mutation_result(
    request_id: str, timeout: float = 60.0
) -> dict[str, object]:
    target = mutation_result_path(request_id)
    payload = wait_for_mutation_payload(target, request_id, timeout)
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


def _archive_many(paths: list[Path]) -> bool:
    """Move one ordered timeline block to History as one recoverable mutation."""
    unique = list(dict.fromkeys(paths))
    if not unique:
        return True

    with timeline_mutation_lock(), _history_order_lock, _queue_order_lock:
        _recover_timeline_intent()
        previous_queue = queue_files_in_order()
        previous_history = history_files_in_order()
        archived_names = {path.name for path in unique}
        desired_queue = [
            path for path in previous_queue if path.name not in archived_names
        ]
        retained_history = [
            path for path in previous_history if path.name not in archived_names
        ]
        desired_history = [
            *(SPOKEN / path.name for path in reversed(unique)),
            *retained_history,
        ]
        planned_moves = [
            (
                path,
                SPOKEN / path.name,
                (SPOKEN / path.name).with_name(
                    f".{path.name}.{os.getpid()}.{time.time_ns()}.duplicate"
                )
                if (SPOKEN / path.name).exists()
                else None,
            )
            for path in unique
        ]
        moves = [
            {
                "source": source.name,
                "target": destination.name,
                "backup": backup.name if backup is not None else None,
            }
            for source, destination, backup in planned_moves
        ]
        intent = {
            "version": 1,
            "operation": "archive_batch",
            "order_version": 2,
            "moves": moves,
            "previous_queue_ids": [public_id_for_path(path) for path in previous_queue],
            "previous_history_ids": [public_id_for_path(path) for path in previous_history],
            "queue_ids": [public_id_for_path(path) for path in desired_queue],
            "history_ids": [public_id_for_path(path) for path in desired_history],
        }
        moved: list[tuple[Path, Path, Path | None]] = []
        history_order_written = False
        queue_order_written = False
        try:
            _write_timeline_intent(intent)
            SPOKEN.mkdir(parents=True, exist_ok=True)
            _write_saved_order(HISTORY_ORDER, intent["history_ids"])
            history_order_written = True
            _write_saved_order(QUEUE_ORDER, intent["queue_ids"])
            queue_order_written = True
            for source, destination, backup in planned_moves:
                if not source.exists():
                    if destination.exists():
                        continue
                    raise FileNotFoundError(source)
                if backup is not None:
                    os.replace(destination, backup)
                moved.append((source, destination, backup))
                os.replace(source, destination)
        except (OSError, RuntimeError) as error:
            rollback_errors: list[str] = []
            for source, destination, backup in reversed(moved):
                try:
                    if not source.exists():
                        os.replace(destination, source)
                    if backup is not None and backup.exists():
                        os.replace(backup, destination)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if queue_order_written:
                try:
                    _write_saved_order(
                        QUEUE_ORDER,
                        [public_id_for_path(path) for path in previous_queue],
                    )
                except (OSError, RuntimeError) as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if history_order_written:
                try:
                    _write_saved_order(
                        HISTORY_ORDER,
                        [public_id_for_path(path) for path in previous_history],
                    )
                except (OSError, RuntimeError) as rollback_error:
                    rollback_errors.append(str(rollback_error))
            invalidate_history()
            if rollback_errors:
                raise MutationOutcomeUnconfirmed(
                    "archive rollback failed: " + "; ".join(rollback_errors)
                ) from error
            try:
                TIMELINE_INTENT.unlink(missing_ok=True)
            except OSError as rollback_error:
                raise MutationOutcomeUnconfirmed(
                    f"archive rollback cleanup failed: {rollback_error}"
                ) from error
            log(f"archive error: {error}")
            return False

        cleanup_complete = True
        for _, _, backup in moved:
            if backup is None:
                continue
            try:
                backup.unlink(missing_ok=True)
            except OSError as cleanup_error:
                cleanup_complete = False
                log(f"archive duplicate cleanup deferred until restart: {cleanup_error}")
        if cleanup_complete:
            try:
                TIMELINE_INTENT.unlink(missing_ok=True)
            except OSError as cleanup_error:
                log(f"archive intent cleanup deferred until restart: {cleanup_error}")
        invalidate_history()
        return True


def archive(path: Path) -> bool:
    return _archive_many([path])


def archive_failed(path: Path) -> bool:
    """Move a chunk that couldn't be synthesized into failed/ so the queue doesn't loop on it."""
    try:
        with timeline_mutation_lock():
            FAILED.mkdir(parents=True, exist_ok=True)
            os.replace(str(path), str(FAILED / path.name))
            try:
                save_queue_order()
            except (OSError, RuntimeError) as order_error:
                log(f"queue order update error after failure {path.name}: {order_error}")
            return True
    except (OSError, RuntimeError) as error:
        log(f"archive_failed error {path.name}: {error}")
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
        self.playing: str | None = None
        self.current_text: str | None = None
        self.current_voice: str | None = None
        self.current_piece = 0
        self.current_piece_count = 0
        self.current_piece_start: int | None = None
        self.current_piece_end: int | None = None
        self.read_failures: dict[str, float] = {}
        self.skip_name: str | None = None  # skipped chunk whose banked pieces must be dropped
        # Selected chunk stays prioritized until its first piece reaches playback
        self.selection_name: str | None = None
        self.stop = threading.Event()    # tell the worker to exit
        self.saw_stop = False            # latched STOP - finish current chunk, then exit
        self.timeline_revision = timeline_revision
        self.timeline_fingerprint = timeline_fingerprint


def command_cancels_graceful_stop(command: Path, st: State) -> bool:
    """Return whether command is newer than any graceful Stop request."""
    if not control_requested(STOP):
        return not st.saw_stop
    try:
        command_time = command.stat().st_mtime_ns
        stop_time = STOP.stat().st_mtime_ns
    except OSError:
        return False
    if stop_time > command_time:
        return False
    try:
        if STOP.stat().st_mtime_ns <= command_time:
            STOP.unlink(missing_ok=True)
    except OSError:
        pass
    st.saw_stop = False
    return True


def consume_ordered_control(signal: Path, st: State) -> bool:
    """Consume a control signal and let a newer graceful Stop win."""
    if not control_requested(signal):
        return False
    accepted = command_cancels_graceful_stop(signal, st)
    signal.unlink(missing_ok=True)
    return accepted


def consume_continue(st: State) -> bool:
    """Consume new-work notice and cancel only an older graceful Stop."""
    if not CONTINUE.exists():
        return False
    accepted = command_cancels_graceful_stop(CONTINUE, st)
    CONTINUE.unlink(missing_ok=True)
    return accepted


_last_status_write = 0.0
_status_failure_started: float | None = None
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
    ensure_identity_catalog()
    with _history_order_lock:
        live = {public_id_for_path(path): path for path in SPOKEN.glob("*.txt")}
        saved_ids = _read_saved_order(HISTORY_ORDER)
        ordered = [
            live.pop(speechicle_id)
            for speechicle_id in saved_ids
            if speechicle_id in live
        ]
        missing = sorted(live.values(), key=history_sort_key, reverse=True)
        return [*missing, *ordered]


def save_history_order(paths: list[Path] | None = None) -> None:
    """Atomically persist the order of archived files."""
    ensure_identity_catalog()
    with _history_order_lock:
        ordered = paths if paths is not None else history_files_in_order()
        live_ids = {public_id_for_path(path) for path in SPOKEN.glob("*.txt")}
        ids = [
            public_id_for_path(path)
            for path in ordered
            if public_id_for_path(path) in live_ids
        ]
        missing = sorted(
            (
                path
                for path in SPOKEN.glob("*.txt")
                if public_id_for_path(path) not in ids
            ),
            key=history_sort_key,
            reverse=True,
        )
        ids.extend(public_id_for_path(path) for path in missing)
        _write_saved_order(HISTORY_ORDER, ids)


def _recover_timeline_intent() -> bool:
    if not TIMELINE_INTENT.exists():
        return False
    try:
        intent = json.loads(TIMELINE_INTENT.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("could not read the pending timeline transaction") from error
    if not isinstance(intent, dict) or intent.get("version") != 1:
        raise RuntimeError("invalid pending timeline transaction")

    operation = intent.get("operation")
    if operation == "identity_migration":
        _apply_identity_migration_intent(intent)
    elif operation == "archive":
        name = intent.get("name")
        previous = intent.get("previous_history_ids")
        desired = intent.get("desired_history_ids")
        if not (
            isinstance(name, str)
            and Path(name).name == name
            and isinstance(previous, list)
            and all(isinstance(item, str) for item in previous)
            and isinstance(desired, list)
            and all(isinstance(item, str) for item in desired)
        ):
            raise RuntimeError("invalid pending archive transaction")
        order_version = 2 if intent.get("order_version") == 2 else 1
        if (QUEUE / name).exists():
            _write_order_payload(HISTORY_ORDER, previous, order_version)
        elif (SPOKEN / name).exists():
            _write_order_payload(HISTORY_ORDER, desired, order_version)
            if order_version == 1:
                queue_ids = [path.stem for path in sorted(QUEUE.glob("*.txt"), key=chunk_sort_key)]
                _write_order_payload(QUEUE_ORDER, queue_ids, 1)
            else:
                save_queue_order()
        else:
            raise RuntimeError(f"pending archive row is missing: {name}")
    elif operation == "archive_batch":
        moves = intent.get("moves")
        queue_ids = intent.get("queue_ids")
        history_ids = intent.get("history_ids")
        if not (
            isinstance(moves, list)
            and all(
                isinstance(move, dict)
                and isinstance(move.get("source"), str)
                and Path(str(move["source"])).name == move["source"]
                and isinstance(move.get("target"), str)
                and Path(str(move["target"])).name == move["target"]
                and (
                    move.get("backup") is None
                    or (
                        isinstance(move.get("backup"), str)
                        and Path(str(move["backup"])).name == move["backup"]
                    )
                )
                for move in moves
            )
            and isinstance(queue_ids, list)
            and all(isinstance(item, str) for item in queue_ids)
            and isinstance(history_ids, list)
            and all(isinstance(item, str) for item in history_ids)
        ):
            raise RuntimeError("invalid pending archive transaction")
        for move in moves:
            source = QUEUE / str(move["source"])
            destination = SPOKEN / str(move["target"])
            backup_name = move.get("backup")
            backup = SPOKEN / str(backup_name) if backup_name is not None else None
            if source.exists():
                os.replace(source, destination)
            elif not destination.exists():
                raise RuntimeError(
                    f"pending archive row is missing: {move['source']}"
                )
            if backup is not None:
                backup.unlink(missing_ok=True)
        order_version = 2 if intent.get("order_version") == 2 else 1
        _write_order_payload(QUEUE_ORDER, queue_ids, order_version)
        _write_order_payload(HISTORY_ORDER, history_ids, order_version)
    elif operation == "promote":
        moves = intent.get("moves")
        queue_ids = intent.get("queue_ids")
        history_ids = intent.get("history_ids")
        if not (
            isinstance(moves, list)
            and all(
                isinstance(move, dict)
                and isinstance(move.get("source"), str)
                and Path(str(move["source"])).name == move["source"]
                and isinstance(move.get("target"), str)
                and Path(str(move["target"])).name == move["target"]
                and (
                    move.get("backup") is None
                    or (
                        isinstance(move.get("backup"), str)
                        and Path(str(move["backup"])).name == move["backup"]
                    )
                )
                for move in moves
            )
            and isinstance(queue_ids, list)
            and all(isinstance(item, str) for item in queue_ids)
            and isinstance(history_ids, list)
            and all(isinstance(item, str) for item in history_ids)
        ):
            raise RuntimeError("invalid pending History promotion transaction")
        for move in moves:
            source_name = str(move["source"])
            target_name = str(move["target"])
            source = SPOKEN / source_name
            target = QUEUE / target_name
            backup_name = move.get("backup")
            backup = SPOKEN / str(backup_name) if backup_name is not None else None
            if target.exists():
                source.unlink(missing_ok=True)
                if backup is not None:
                    backup.unlink(missing_ok=True)
                continue
            renamed_source = QUEUE / source_name
            if source.exists():
                os.replace(source, target)
            elif backup is not None and backup.exists():
                os.replace(backup, target)
            elif renamed_source.exists():
                os.replace(renamed_source, target)
            else:
                raise RuntimeError(
                    f"pending History promotion row is missing: {source_name}"
                )
            if backup is not None:
                backup.unlink(missing_ok=True)
        order_version = 2 if intent.get("order_version") == 2 else 1
        _write_order_payload(QUEUE_ORDER, queue_ids, order_version)
        _write_order_payload(HISTORY_ORDER, history_ids, order_version)
    else:
        raise RuntimeError("unknown pending timeline transaction")

    TIMELINE_INTENT.unlink()
    invalidate_history()
    log(f"recovered pending timeline {operation} transaction")
    return True


def repair_interrupted_timeline_transition() -> None:
    """Repair incomplete or legacy file layouts before publishing the timeline."""
    with timeline_mutation_lock(), _history_order_lock, _queue_order_lock:
        _recover_timeline_intent()
        _ensure_identity_catalog_unlocked()
        invalidate_history()


def history_snapshot() -> tuple[int, list[dict[str, object]]]:
    """Return the cached bounded archive view, refreshing when invalidated."""
    global _history_dirty, _history_count, _history_items
    with _history_order_lock:
        if _history_dirty:
            history_files = history_files_in_order()
            items: list[dict[str, object]] = []
            read_failed = False
            previous = {str(item["id"]): item for item in _history_items}
            for path in history_files[:HISTORY_LIMIT]:
                speechicle_id = public_id_for_path(path)
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except OSError:
                    read_failed = True
                    text = str(previous.get(speechicle_id, {}).get("text", ""))
                items.append(
                    {
                        "id": speechicle_id,
                        "text": text,
                        "voice": voice_from_name(path.name),
                    }
                )
            _history_count = len(history_files)
            _history_items = items
            _history_dirty = read_failed
        return _history_count, _history_items


def activate_next_chunk(st: State) -> bool:
    """Give queued work one current item before publishing or synthesizing it."""
    with st.lock:
        if st.playing or st.saw_stop or st.stop.is_set():
            return False
        try:
            ordered = queue_files_in_order()
        except RuntimeError as error:
            log(str(error))
            return False
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
        st.playing = current.name
        st.current_voice = voice_from_name(current.name)
        st.current_piece = 0
        st.current_piece_start = None
        st.current_piece_end = None
        try:
            text = current.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        st.current_text = text
        st.current_piece_count = len(split_text(text, SPLIT_CHARS))
        return True


def timeline_fingerprint(
    current: dict[str, object] | None,
    queue_items: list[dict[str, object]],
    history_count: int,
    history_items: list[dict[str, object]],
) -> str:
    """Hash the ordered timeline fields that define user-visible row placement."""
    ordered_rows = {
        "current": (
            {"id": current.get("id"), "voice": current.get("voice")}
            if current is not None
            else None
        ),
        "queue": [
            {"id": item.get("id"), "voice": item.get("voice")}
            for item in queue_items
        ],
        "history_count": history_count,
        "history": [
            {"id": item.get("id"), "voice": item.get("voice")}
            for item in history_items
        ],
    }
    encoded = json.dumps(ordered_rows, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _advance_timeline_revision(st: State, fingerprint: str) -> int:
    """Update the one revision counter owned by status publication."""
    with st.lock:
        previous = st.timeline_fingerprint
        if previous is None:
            st.timeline_fingerprint = fingerprint
        elif previous != fingerprint:
            st.timeline_fingerprint = fingerprint
            st.timeline_revision += 1
        return st.timeline_revision


def fingerprint_from_status(status: object) -> str | None:
    """Rebuild the persisted fingerprint from one valid status snapshot."""
    if not _snapshot_is_valid(status):
        return None
    assert isinstance(status, dict)
    current = status.get("current")
    queue_items = status.get("queue")
    history_items = status.get("history")
    history_count = status.get("history_count")
    if not (
        (current is None or isinstance(current, dict))
        and isinstance(queue_items, list)
        and all(isinstance(item, dict) for item in queue_items)
        and isinstance(history_items, list)
        and all(isinstance(item, dict) for item in history_items)
        and isinstance(history_count, int)
    ):
        return None
    return timeline_fingerprint(current, queue_items, history_count, history_items)


def load_timeline_revision_seed() -> tuple[int, str | None]:
    """Load the last valid revision before the engine replaces its status file."""
    try:
        stored_status = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0, None
    fingerprint = fingerprint_from_status(stored_status)
    revision = stored_status.get("timeline_revision") if isinstance(stored_status, dict) else None
    if fingerprint is None or not isinstance(revision, int) or revision < 0:
        return 0, None
    return revision, fingerprint


def publish_status(
    playback_state: str,
    st: State,
    *,
    playback: PauseableAudio | None = None,
    sample_rate: int | None = None,
    force: bool = False,
) -> dict[str, object] | None:
    """Publish an atomic runtime snapshot for the desktop controller."""
    global _last_status_write, _status_failure_started
    now = time.time()
    if not force and now - _last_status_write < 0.25:
        return None

    activate_next_chunk(st)
    with st.lock:
        playing = st.playing
        selected = st.selection_name
        current_text = st.current_text
        current_voice = st.current_voice
        current_piece = st.current_piece
        current_piece_count = st.current_piece_count
        current_piece_start = st.current_piece_start
        current_piece_end = st.current_piece_end

    try:
        ordered_queue = queue_files_in_order()
    except RuntimeError as error:
        log(str(error))
        return None
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
            text = ""
        queue_items.append(
            {
                "id": public_id_for_path(path),
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
            current_piece_start = None
            current_piece_end = None
        current = {
            "id": public_id_for_path(current_path),
            "text": current_text or "",
            "voice": current_voice or voice_from_name(current_path.name),
            "piece": current_piece,
            "piece_count": current_piece_count,
            "piece_start": current_piece_start,
            "piece_end": current_piece_end,
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

    payload = {
        "version": STATUS_VERSION,
        "timeline_revision": timeline_revision,
        "state": playback_state,
        "updated_at": now,
        "engine_pid": os.getpid(),
        "current": current,
        "queue_count": len(queue_items),
        "queue": queue_items,
        "history_count": history_count,
        "history": history_items,
    }
    temp_path = STATUS.with_name(
        f"{STATUS.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, STATUS)
        STATUS_FAILURE.unlink(missing_ok=True)
        _last_status_write = now
        if _status_failure_started is not None:
            log(
                "status publication recovered after "
                f"{now - _status_failure_started:.1f} seconds"
            )
        _status_failure_started = None
        return payload
    except OSError as error:
        if _status_failure_started is None:
            _status_failure_started = now
            log(f"status publication failed: {type(error).__name__}: {error}")
        elif now - _status_failure_started >= 5.0:
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


def _find_chunk(directory: Path, chunk_id: str) -> Path | None:
    return _path_for_public_id(directory, chunk_id)


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


def _replace_queue_voice_unlocked(source: Path, voice: str) -> Path:
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


def _replace_queue_voice(source: Path, voice: str) -> Path:
    with timeline_mutation_lock():
        return _replace_queue_voice_unlocked(source, voice)


def _promote_history_selection_unlocked(
    source: Path,
    voice: str | None = None,
) -> tuple[Path, int]:
    """Move the playback boundary to one History item without changing display order."""
    with _history_order_lock, _queue_order_lock:
        _recover_timeline_intent()
        history = history_files_in_order()
        try:
            selected_index = history.index(source)
        except ValueError as error:
            raise ValueError(f"history chunk not found: {source.stem}") from error

        promoted = history[: selected_index + 1]
        remaining_history = history[selected_index + 1 :]
        previous_queue = queue_files_in_order()
        selected_source = promoted[-1]
        selected_target = QUEUE / selected_source.name
        if voice and voice != voice_from_name(selected_source.name):
            selected_target = _voice_variant_path(selected_target, voice)
            if selected_target.exists():
                raise RuntimeError(
                    f"voice target already exists: {selected_target.stem}"
                )
        targets = [
            selected_target if archived == selected_source else QUEUE / archived.name
            for archived in promoted
        ]
        playback_order = list(reversed(targets))
        target_names = {path.name for path in targets}
        remaining_queue = [
            path for path in previous_queue if path.name not in target_names
        ]
        planned_moves = [
            (
                archived,
                queued,
                archived.with_name(
                    f".{archived.name}.{os.getpid()}.{time.time_ns()}.duplicate"
                )
                if queued.exists()
                else None,
            )
            for archived, queued in zip(promoted, targets)
        ]
        moved: list[tuple[Path, Path, Path | None]] = []
        try:
            _write_timeline_intent(
                {
                    "version": 1,
                    "operation": "promote",
                    "moves": [
                        {
                            "source": archived.name,
                            "target": target.name,
                            "backup": backup.name if backup is not None else None,
                        }
                        for archived, target, backup in planned_moves
                    ],
                    "queue_ids": [
                        public_id_for_path(path)
                        for path in [*playback_order, *remaining_queue]
                    ],
                    "history_ids": [
                        public_id_for_path(path) for path in remaining_history
                    ],
                    "order_version": 2,
                }
            )
            for archived, queued, duplicate_backup in planned_moves:
                if duplicate_backup is not None:
                    os.replace(archived, duplicate_backup)
                else:
                    os.replace(archived, queued)
                moved.append((archived, queued, duplicate_backup))
            save_queue_order([*playback_order, *remaining_queue])
            save_history_order(remaining_history)
            cleanup_complete = True
            for _, _, duplicate_backup in moved:
                if duplicate_backup is not None:
                    try:
                        duplicate_backup.unlink(missing_ok=True)
                    except OSError as cleanup_error:
                        cleanup_complete = False
                        log(
                            f"could not remove legacy History duplicate "
                            f"{duplicate_backup.name}: {cleanup_error}"
                        )
            if cleanup_complete:
                try:
                    TIMELINE_INTENT.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    log(
                        "History promotion intent cleanup deferred until restart: "
                        f"{cleanup_error}"
                    )
        except (OSError, RuntimeError, ValueError) as error:
            recovery_errors = []
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
            except (OSError, RuntimeError) as recovery_error:
                recovery_errors.append(str(recovery_error))
            if recovery_errors:
                raise MutationOutcomeUnconfirmed(
                    "History boundary rollback failed: "
                    + "; ".join(recovery_errors)
                ) from error
            try:
                TIMELINE_INTENT.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise MutationOutcomeUnconfirmed(
                    f"History rollback cleanup failed: {cleanup_error}"
                ) from error
            raise RuntimeError("could not move History playback boundary") from error
        finally:
            invalidate_history()
    return selected_target, len(promoted)


def promote_history_selection(
    source: Path,
    voice: str | None = None,
) -> tuple[Path, int]:
    with timeline_mutation_lock():
        return _promote_history_selection_unlocked(source, voice)


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
        current_generation = st.claims.get(st.playing) if st.playing else None
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
        kept_names = {entry[0].name for entry in kept}
        st.claims = {
            name: generation
            for name, generation in st.claims.items()
            if name in kept_names
        }
        if st.playing and current_generation is not None:
            st.claims[st.playing] = current_generation
        if st.selection_name and not (QUEUE / st.selection_name).exists():
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
    if not _archive_many(archived):
        raise RuntimeError("could not archive older waiting chunks")
    return archived


def apply_play_mutation(
    buf: "queue.Queue",
    st: State,
    request: PlayMutation,
) -> str | None:
    """Select one ID while excluding worker claims from the transaction."""
    with st.lock:
        return _apply_play_mutation_locked(buf, st, request)


def _apply_play_mutation_locked(
    buf: "queue.Queue",
    st: State,
    request: PlayMutation,
) -> str | None:
    ensure_identity_catalog()
    chunk_id = request.id
    requested_voice = request.voice

    # Discard audio rendered for the old order but keep source files queued for a clean restart
    with st.lock:
        playing = st.playing
    if (
        playing
        and public_id_for_path(QUEUE / playing) == chunk_id
        and (requested_voice is None or requested_voice == voice_from_name(playing))
    ):
        PAUSE.unlink(missing_ok=True)
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
                st.stop.set()
                raise
            except (OSError, RuntimeError, ValueError) as error:
                log(f"could not replay {chunk_id}: {error}")
                raise RuntimeError(f"could not replay {chunk_id}") from error
    if target is None:
        log(f"PLAY ignored; chunk not found: {chunk_id}")
        raise ValueError(f"chunk not found: {chunk_id}")

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
            st.stop.set()
            raise
        except (OSError, RuntimeError, ValueError) as error:
            log(f"could not change voice for {chunk_id}: {error}")
            raise RuntimeError(f"could not change voice for {chunk_id}") from error

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
                st.stop.set()
                raise MutationOutcomeUnconfirmed(
                    "play command result was unconfirmed"
                ) from error
            log(f"could not select {chunk_id}: {error}")
            raise RuntimeError(f"could not select {chunk_id}") from error

    PAUSE.unlink(missing_ok=True)
    try:
        selected_text = target.read_text(encoding="utf-8").strip()
    except OSError:
        selected_text = ""
    if not target.exists():
        st.stop.set()
        raise MutationOutcomeUnconfirmed("play command result was unconfirmed")
    with st.lock:
        discarded = _discard_buffer(buf)
        st.claims.clear()
        st.playing = target.name
        st.current_text = selected_text
        st.current_voice = voice_from_name(target.name)
        st.current_piece = 0
        st.current_piece_count = len(split_text(selected_text, SPLIT_CHARS))
        st.current_piece_start = None
        st.current_piece_end = None
        st.selection_name = target.name
        st.skip_name = None
        st.saw_stop = False
    if public_id_for_path(target) != chunk_id:
        st.stop.set()
        raise MutationOutcomeUnconfirmed("play changed the stable Speechicle ID")
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


def do_clear(buf: "queue.Queue", st: State) -> bool:
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
        st.selection_name = None
        st.skip_name = None
        st.saw_stop = False
        clear_current_playback(st)
        try:
            PAUSE.unlink(missing_ok=True)
            STOP.unlink(missing_ok=True)
        except OSError as error:
            log(f"could not clear a playback marker: {error}")
    log(f"clear; archived {len(ordered)} row(s), dropped {discarded} piece(s)")
    return True


def apply_queue_command(
    buf: "queue.Queue",
    st: State,
    action: str,
    chunk_id: str,
    before_id: str | None,
) -> None:
    """Apply one queue or History mutation without changing current playback."""
    ensure_identity_catalog()
    if action == "delete":
        with st.lock:
            playing_id = (
                public_id_for_path(QUEUE / st.playing) if st.playing else None
            )
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
            except (OSError, RuntimeError) as order_error:
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
            source = next(
                (
                    path
                    for path in ordered_history
                    if public_id_for_path(path) == chunk_id
                ),
                None,
            )
            if source is None:
                raise ValueError(f"history chunk not found: {chunk_id}")
            if before_id == chunk_id:
                return
            ordered_history.remove(source)
            if before_id is None:
                ordered_history.insert(min(HISTORY_LIMIT - 1, len(ordered_history)), source)
            else:
                destination = next(
                    (
                        path
                        for path in ordered_history
                        if public_id_for_path(path) == before_id
                    ),
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
    source = next(
        (path for path in ordered if public_id_for_path(path) == chunk_id),
        None,
    )
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
            (
                path
                for path in ordered
                if public_id_for_path(path) == before_id and path.name != playing
            ),
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


def _publish_mutation_outcome(
    claimed: Path,
    st: State,
    request_id: str,
    outcome: str,
    *,
    result_id: str | None = None,
    error: str | None = None,
    playback_state: str | None = None,
) -> bool:
    """Publish the authoritative status before its matching mutation result."""
    snapshot = publish_status(
        playback_state
        or ("stopped" if outcome == "unconfirmed" and st.stop.is_set() else "idle"),
        st,
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


def process_mutation_requests(
    buf: "queue.Queue",
    st: State,
    held_chunk_name: str | None = None,
) -> str | None:
    """Apply mutations and publish their snapshots in one total FIFO order."""
    prune_mutation_results()
    effect: str | None = None
    invalidate_held_chunk = False
    while not st.stop.is_set():
        claimed = claim_next_mutation_request()
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
        if not command_cancels_graceful_stop(claimed, st):
            _publish_mutation_outcome(
                claimed,
                st,
                request.request_id,
                "rejected",
                error="engine stopped before mutation was applied",
            )
            continue

        mutation_subject = (
            request.request_id if isinstance(request, ClearMutation) else request.id
        )
        result_id: str | None = None
        try:
            if isinstance(request, PlayMutation):
                play_effect = apply_play_mutation(buf, st, request)
                effect = play_effect or effect
                invalidate_held_chunk = invalidate_held_chunk or play_effect == "select"
                result_id = request.id
            elif isinstance(request, ClearMutation):
                if not do_clear(buf, st):
                    raise MutationOutcomeUnconfirmed("clear result was unconfirmed")
                effect = "clear"
                invalidate_held_chunk = True
            elif isinstance(request, MoveMutation):
                action = "move_history" if request.section == "history" else "move"
                apply_queue_command(
                    buf,
                    st,
                    action,
                    request.id,
                    request.before_id,
                )
                if request.section == "waiting":
                    if effect is None:
                        effect = "queue_changed"
                    invalidate_held_chunk = True
            elif isinstance(request, ArchiveMutation):
                apply_queue_command(buf, st, "archive", request.id, None)
                if effect is None:
                    effect = "queue_changed"
                invalidate_held_chunk = True
            elif isinstance(request, DeleteMutation):
                apply_queue_command(buf, st, "delete", request.id, None)
            else:
                assert_never(request)
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
    if invalidate_held_chunk and held_chunk_name is not None:
        invalidate_claim(st, held_chunk_name)
    return effect


def reject_pending_requests(reason: str, st: State) -> None:
    """Give every published mutation a terminal result before the engine exits."""
    while not st.stop.is_set():
        claimed = claim_next_mutation_request()
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
            playback_state="stopped",
        ):
            return


def _claimed(st: State, name: str, generation: int | None = None) -> bool:
    with st.lock:
        return (
            name in st.claims
            and st.skip_name != name
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
        if st.selection_name == name:
            st.selection_name = None
        if st.playing == name:
            clear_current_playback(st)


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
            active = next(
                (path for path in candidates if path.name == st.playing),
                None,
            )
            if active is not None and active.name not in st.claims:
                return _record_claim(st, active)
            return None
        if st.selection_name:
            selected = next(
                (path for path in candidates if path.name == st.selection_name),
                None,
            )
            if selected is not None:
                candidates.remove(selected)
                candidates.insert(0, selected)
                if selected.name not in st.claims:
                    return _record_claim(st, selected)
            else:
                st.selection_name = None
        if st.playing:
            active = next(
                (path for path in candidates if path.name == st.playing),
                None,
            )
            if active is not None and active.name not in st.claims:
                return _record_claim(st, active)
        for path in candidates:
            if path.name in st.claims:
                continue
            return _record_claim(st, path)
    return None


def claim_next_queued_chunk(st: State) -> Path | None:
    """Claim the next item while hiding the worker-only claim generation."""
    claim = claim_next_queued_chunk_with_generation(st)
    return claim[0] if claim is not None else None


def synth_worker(kokoro, buf: "queue.Queue", st: State) -> None:
    """Producer: claim the next queued chunk, split it into sentence-aligned pieces,
    synthesize each, and bank playback entries in buf (blocking when the buffer
    is full - natural backpressure). Abandons a chunk's
    remaining pieces when clear or skip unclaims it."""
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
        except Exception as e:
            log(f"read error {nxt.name}: {e}")
            with st.lock:
                if st.claims.get(nxt.name) != generation:
                    continue
                st.claims.pop(nxt.name)
                started = st.read_failures.setdefault(nxt.name, time.monotonic())
                deadline = 0.5 if st.saw_stop else 5.0
                if time.monotonic() - started >= deadline:
                    if st.playing == nxt.name:
                        clear_current_playback(st)
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
        delivered = 0
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
                if delivered == 0:
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
                # Mid-chunk failure: deliver an empty terminal piece so the
                # consumer still archives and releases the chunk.
                audio = audio[:0]
                terminal_failure = True
            else:
                log(
                    f"synth {nxt.name}[{idx+1}/{len(pieces)}] voice={voice} "
                    f"chars={len(piece.text)} synth={time.time()-t0:.1f}s audio={len(audio)/sr:.1f}s"
                )
            entry = (
                nxt,
                audio,
                sr,
                idx == 0,
                terminal_failure or idx == len(pieces) - 1,
                idx + 1,
                len(pieces),
                text,
                voice,
                piece.start,
                piece.end,
                generation,
            )
            while not st.stop.is_set():
                with st.lock:
                    if (
                        st.claims.get(nxt.name) != generation
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
            if terminal_failure:
                break


def gap_wait(seconds: float, buf: "queue.Queue", st: State) -> str | None:
    """Wait for an audible gap without counting time spent paused."""
    remaining = seconds
    last_tick = time.monotonic()
    while remaining > 0:
        if st.stop.is_set():
            return "fatal"
        consume_continue(st)
        if consume_control(INTERRUPT):
            reject_pending_requests("engine interrupted before command was applied", st)
            return "interrupt"
        with st.lock:
            held_chunk_name = st.playing
        mutation_effect = process_mutation_requests(buf, st, held_chunk_name)
        if mutation_effect in {"select", "clear"}:
            return mutation_effect
        if mutation_effect == "queue_changed":
            return "queue_changed"
        if consume_control(STOP):
            reject_pending_requests("engine stopped before command was applied", st)
            return "stop"
        if st.stop.is_set():
            return "fatal"
        if consume_control(SKIP):
            return "skip"
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
                stream.abort()
                reject_pending_requests(
                    "engine interrupted before command was applied", st
                )
                return "interrupt"
            consume_continue(st)
            mutation_effect = process_mutation_requests(buf, st)
            if mutation_effect == "select":
                stream.abort()
                return "select"
            if mutation_effect == "clear":
                stream.abort()
                return "clear"
            if not st.saw_stop and control_requested(STOP):
                st.saw_stop = True
            if st.saw_stop:
                reject_pending_requests("engine is stopping", st)
            if st.stop.is_set():
                stream.abort()
                return "fatal"
            if consume_control(SKIP):
                stream.abort()
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
    released = outcome in {"select", "clear", "fatal"} or archive(path)
    with st.lock:
        if released:
            st.claims.pop(path.name, None)
        else:
            st.stop.set()
            log(
                f"could not archive {path.name}; stopping so it can be retried"
            )
        if st.playing == path.name:
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
    st.current_piece_start = None
    st.current_piece_end = None


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
    repair_interrupted_timeline_transition()
    st = State(timeline_revision, timeline_fingerprint_seed)
    publish_status("loading", st, force=True)
    settle_stale_mutation_claims(st)
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
                playing = st.playing
            if not playing and (st.saw_stop or consume_control(STOP)):
                reject_pending_requests(
                    "engine stopped before command was applied", st
                )
                log("STOP (idle); exiting")
                return
            if playing and not st.saw_stop and control_requested(STOP):
                st.saw_stop = True
            if st.saw_stop:
                reject_pending_requests("engine is stopping", st)
            if st.stop.is_set():
                log("engine stopping after a storage failure")
                return
            if consume_control(SKIP):
                with st.lock:
                    current_name = st.playing
                current_path = QUEUE / current_name if current_name else None
                if current_path is not None and current_path.exists():
                    finish_chunk_playback(current_path, "skip", True, st)
                    publish_status("idle", st, force=True)
                    log(f"SKIP before playback {current_path.name}")
                    continue
                log("SKIP ignored (no Current)")

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
                    piece_start,
                    piece_end,
                    claim_generation,
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
                stale = buffered_piece_is_stale(
                    st,
                    path.name,
                    claim_generation,
                )
                selected = not stale and first and st.selection_name == path.name
                if first and st.skip_name and not stale:
                    st.skip_name = None
            if stale:
                continue

            mutation_effect = process_mutation_requests(buf, st, path.name)
            if mutation_effect is not None:
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
                    invalidate_claim(st, path.name)
                    continue
                if outcome == "stop":
                    invalidate_claim(st, path.name)
                    log("STOP (gap); exiting")
                    return
                if outcome == "clear":
                    continue
                if outcome == "queue_changed":
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
                st.current_piece_start = piece_start
                st.current_piece_end = piece_end
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
                if consume_continue(st):
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
        CONTINUE,
        WARMUP,
    ):
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
    instance_lock = EngineInstanceLock()
    if not instance_lock.acquire():
        return
    try:
        timeline_revision, timeline_fingerprint_seed = load_timeline_revision_seed()
        STATUS.unlink(missing_ok=True)
        clear_transient_signals()
        run_engine_loop(timeline_revision, timeline_fingerprint_seed)
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


def _contains_private_status_field(value: object) -> bool:
    if isinstance(value, dict):
        return "filename" in value or any(
            _contains_private_status_field(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_private_status_field(item) for item in value)
    return False


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
            if (
                isinstance(stored_status, dict)
                and stored_status.get("version") == STATUS_VERSION
                and not _contains_private_status_field(stored_status)
            ):
                print(json.dumps(stored_status, ensure_ascii=False))
                return
            break
    if STATUS.exists() and status_error is not None:
        raise RuntimeError(f"could not read engine status: {status_error}")
    ensure_identity_catalog()
    queue_items = []
    for path in queue_files_in_order():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        queue_items.append(
            {
                "id": public_id_for_path(path),
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
            "piece_start": None,
            "piece_end": None,
            "elapsed_seconds": 0.0,
        }
    print(
        json.dumps(
            {
                "version": STATUS_VERSION,
                "timeline_revision": 0,
                "state": "stopped",
                "updated_at": 0,
                "engine_pid": None,
                "current": current,
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
    mutate = commands.add_parser("mutate", help=argparse.SUPPRESS)
    mutate.add_argument("mutation_json", help=argparse.SUPPRESS)
    commands.add_parser("skip", help="skip the current chunk")
    commands.add_parser("clear", help="move Current and Waiting speech to History")
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
            print(public_id_for_path(queued))
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
            request_id = request_mutation(
                build_mutation_request("play", id=args.chunk_id, voice=args.voice)
            )
            print(json.dumps(wait_for_mutation_result(request_id), ensure_ascii=False))
        elif args.command in {"move", "move-history", "archive", "delete"}:
            start_engine()
            if args.command in {"move", "move-history"}:
                request = build_mutation_request(
                    "move",
                    section="history" if args.command == "move-history" else "waiting",
                    id=args.chunk_id,
                    before_id=args.before_id,
                )
            else:
                request = build_mutation_request(args.command, id=args.chunk_id)
            request_id = request_mutation(request)
            print(
                json.dumps(
                    wait_for_mutation_result(request_id),
                    ensure_ascii=False,
                )
            )
        elif args.command == "mutate":
            request_id = secrets.token_hex(12)
            try:
                mutation_payload = json.loads(args.mutation_json)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid mutation JSON: {error}") from error
            request = parse_cli_mutation(mutation_payload, request_id)
            start_engine()
            request_mutation(request)
            print(json.dumps(wait_for_mutation_result(request_id), ensure_ascii=False))
        elif args.command == "clear":
            start_engine()
            request_id = request_mutation(build_mutation_request("clear"))
            print(json.dumps(wait_for_mutation_result(request_id), ensure_ascii=False))
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
