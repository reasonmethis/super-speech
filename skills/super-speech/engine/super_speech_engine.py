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
  PLAY.*.json - select a queued or recent chunk by ID; selecting another chunk
                preempts the current one without marking it spoken
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
PLAY = BASE / "PLAY.json"
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

ENGINE_VERSION = "0.4.1"
STATUS_VERSION = 3


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


def _next_chunk_number() -> int:
    QUEUE.mkdir(parents=True, exist_ok=True)
    SPOKEN.mkdir(parents=True, exist_ok=True)
    existing = [*QUEUE.glob("*.txt"), *SPOKEN.glob("*.txt")]
    numbers = [
        int(path.name.split("-", 1)[0])
        for path in existing
        if path.name.split("-", 1)[0].isdigit()
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
        try:
            try:
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as chunk:
                    chunk.write(text)
                return path
            except Exception:
                path.unlink(missing_ok=True)
                raise
        finally:
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


def chunk_sort_key(path: Path) -> tuple[int, str]:
    prefix = path.name.split("-", 1)[0]
    return (int(prefix) if prefix.isdigit() else sys.maxsize, path.name)


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


def play_ack_path(request_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", request_id):
        raise ValueError("invalid play request ID")
    return BASE / f"PLAY_ACK.{request_id}.json"


def request_play(chunk_id: str) -> str:
    """Atomically publish one uniquely acknowledged selection request."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", chunk_id):
        raise ValueError("chunk ID contains invalid characters")
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
            json.dumps({"id": chunk_id, "request_id": request_id}), encoding="utf-8"
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


def read_play_claim(claimed: Path) -> tuple[str, str] | None:
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        chunk_id = payload.get("id")
        request_id = payload.get("request_id")
        if not isinstance(chunk_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", chunk_id
        ):
            raise ValueError("invalid chunk ID")
        if not isinstance(request_id, str):
            raise ValueError("invalid play request ID")
        play_ack_path(request_id)
        return chunk_id, request_id
    except (OSError, ValueError, json.JSONDecodeError) as error:
        log(f"invalid play request: {error}")
        return None
    finally:
        claimed.unlink(missing_ok=True)


def take_play_request() -> tuple[str, str] | None:
    prune_play_acknowledgements()
    claimed = claim_play_requests()
    if not claimed:
        return None
    for superseded in claimed[:-1]:
        request = read_play_claim(superseded)
        if request is not None:
            _, request_id = request
            publish_play_ack(request_id, error="superseded by a newer play request")
    return read_play_claim(claimed[-1])


def publish_play_ack(
    request_id: str, *, result_id: str | None = None, error: str | None = None
) -> None:
    target = play_ack_path(request_id)
    temp_path = target.with_name(f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    payload = {
        "ok": error is None,
        "result_id": result_id,
        "accepted_at": time.time(),
        "error": error,
    }
    try:
        temp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp_path, target)
    except OSError as ack_error:
        log(f"could not publish play acknowledgement: {ack_error}")
    finally:
        temp_path.unlink(missing_ok=True)


def wait_for_play_ack(request_id: str, timeout: float = 60.0) -> dict[str, object]:
    target = play_ack_path(request_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        except (OSError, ValueError, json.JSONDecodeError) as error:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"invalid engine acknowledgement: {error}") from error
        target.unlink(missing_ok=True)
        if payload.get("ok") is not True:
            raise RuntimeError(str(payload.get("error") or "engine rejected play request"))
        result_id = payload.get("result_id")
        accepted_at = payload.get("accepted_at")
        if not isinstance(result_id, str) or not isinstance(accepted_at, (int, float)):
            raise RuntimeError("engine returned an incomplete play acknowledgement")
        return {"id": result_id, "accepted_at": accepted_at}
    raise RuntimeError("engine did not acknowledge play request")


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


def archive(path: Path) -> bool:
    destination = SPOKEN / path.name
    try:
        SPOKEN.mkdir(parents=True, exist_ok=True)
        os.replace(str(path), str(destination))
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
        self.playing: str | None = None  # filename currently playing (never reclaim/clear)
        self.current_text: str | None = None
        self.current_voice: str | None = None
        self.current_piece = 0
        self.current_piece_count = 0
        self.skip_name: str | None = None  # skipped chunk whose banked pieces must be dropped
        # Selected chunk stays prioritized until its first piece reaches playback
        self.selection_name: str | None = None
        self.stop = threading.Event()    # tell the worker to exit
        self.saw_stop = False            # latched STOP — finish current chunk, then exit


_last_status_write = 0.0
_history_lock = threading.Lock()
_history_dirty = True
_history_count = 0
_history_items: list[dict[str, object]] = []


def invalidate_history() -> None:
    global _history_dirty
    with _history_lock:
        _history_dirty = True


def history_snapshot() -> tuple[int, list[dict[str, object]]]:
    """Return the cached bounded archive view, refreshing only after an archive move."""
    global _history_dirty, _history_count, _history_items
    with _history_lock:
        if _history_dirty:
            history_files = sorted(SPOKEN.glob("*.txt"), key=chunk_sort_key, reverse=True)
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

    with st.lock:
        playing = st.playing
        current_text = st.current_text
        current_voice = st.current_voice
        current_piece = st.current_piece
        current_piece_count = st.current_piece_count

    queue_files = [
        path
        for path in sorted(QUEUE.glob("*.txt"), key=chunk_sort_key)
        if path.name != playing
    ]
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
    if playing:
        current = {
            "id": Path(playing).stem,
            "filename": playing,
            "text": current_text or "",
            "voice": current_voice or voice_from_name(playing),
            "piece": current_piece,
            "piece_count": current_piece_count,
            "elapsed_seconds": (
                playback.position / sample_rate
                if playback is not None and sample_rate
                else 0.0
            ),
        }

    if PAUSE.exists() and playback_state in {"idle", "playing", "ready"}:
        playback_state = "paused"
    history_count, history_items = history_snapshot()

    payload = {
        "version": STATUS_VERSION,
        "state": playback_state,
        "updated_at": now,
        "engine_pid": os.getpid(),
        "current": current,
        "queue_count": len(queue_files),
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


def _copy_history_to_queue(source: Path) -> Path:
    try:
        filename_tail = source.name.split("-", 1)[1]
    except IndexError as error:
        raise ValueError(f"invalid archived chunk filename: {source.name}") from error
    return _reserve_queue_file(filename_tail, source.read_text(encoding="utf-8"))


def _discard_buffer(buf: "queue.Queue") -> int:
    discarded = 0
    while True:
        try:
            buf.get_nowait()
            discarded += 1
        except queue.Empty:
            return discarded


def process_play_request(buf: "queue.Queue", st: State) -> str | None:
    """Resolve one selected ID and return ``select`` when playback must yield."""
    request = take_play_request()
    if request is None:
        return None
    chunk_id, request_id = request

    # Discard audio rendered for the old order but keep source files queued for a clean restart
    with st.lock:
        playing = st.playing
    if playing and Path(playing).stem == chunk_id:
        STOP.unlink(missing_ok=True)
        PAUSE.unlink(missing_ok=True)
        with st.lock:
            st.saw_stop = False
        publish_play_ack(request_id, result_id=chunk_id)
        log(f"PLAY current {chunk_id}; resumed")
        return None

    target = _find_chunk(QUEUE, chunk_id)
    replayed_from: Path | None = None
    if target is None:
        replayed_from = _find_chunk(SPOKEN, chunk_id)
        if replayed_from is not None:
            try:
                target = _copy_history_to_queue(replayed_from)
            except (OSError, RuntimeError, ValueError) as error:
                log(f"could not replay {chunk_id}: {error}")
                publish_play_ack(request_id, error=f"could not replay {chunk_id}")
                return None
    if target is None:
        log(f"PLAY ignored; chunk not found: {chunk_id}")
        publish_play_ack(request_id, error=f"chunk not found: {chunk_id}")
        return None

    STOP.unlink(missing_ok=True)
    PAUSE.unlink(missing_ok=True)
    with st.lock:
        discarded = _discard_buffer(buf)
        st.claimed.clear()
        st.selection_name = target.name
        st.skip_name = None
        st.saw_stop = False
    publish_play_ack(request_id, result_id=target.stem)
    if replayed_from is None:
        log(f"PLAY selected {target.name}; discarded {discarded} banked piece(s)")
    else:
        log(
            f"PLAY replay {replayed_from.name} as {target.name}; "
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
                if drop_to_spoken(entry[0]):
                    n += 1
                else:
                    blocked.add(entry[0].name)
        for entry in keep:
            buf.put_nowait(entry)
        for f in glob.glob(str(QUEUE / "*.txt")):
            if os.path.basename(f) == st.playing:
                continue
            path = Path(f)
            if drop_to_spoken(path):
                n += 1
            else:
                blocked.add(path.name)
        st.claimed = blocked | ({st.playing} if st.playing else set())
        st.selection_name = None
    log(f"CLEAR; dropped {n} buffered/queued chunk(s)")


def _claimed(st: State, name: str) -> bool:
    with st.lock:
        return (name in st.claimed or name == st.playing) and st.skip_name != name


def claim_next_queued_chunk(st: State) -> Path | None:
    with st.lock:
        candidates = sorted(QUEUE.glob("*.txt"), key=chunk_sort_key)
        if st.selection_name:
            selected = next(
                (path for path in candidates if path.name == st.selection_name),
                None,
            )
            if selected is not None:
                candidates.remove(selected)
                candidates.insert(0, selected)
            else:
                st.selection_name = None
        for path in candidates:
            if path.name in st.claimed or path.name == st.playing:
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
            with st.lock:
                st.claimed.discard(nxt.name)
            continue
        if not text:
            log(f"empty {nxt.name}, archiving")
            if archive(nxt):
                with st.lock:
                    st.claimed.discard(nxt.name)
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
                        with st.lock:
                            st.claimed.discard(nxt.name)
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
        if process_play_request(buf, st) == "select":
            return "select"
        if consume(INTERRUPT):
            return "interrupt"
        if consume(SKIP):
            return "skip"
        if consume(STOP):
            return "stop"
        if consume(CLEAR):
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
    if _prev_audio_end is not None:
        log(f"boundary kind={kind} silence={(t0 - _prev_audio_end)*1000:.0f}ms before {path.name}")

    last_position = playback.position
    last_progress = time.monotonic()
    stalled = False
    try:
        while not playback.done.wait(SIGNAL_TICK):
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
            if consume(INTERRUPT):
                stream.abort()
                return "interrupt"
            if process_play_request(buf, st) == "select":
                stream.abort()
                return "select"
            if consume(SKIP):
                stream.abort()
                return "skip"
            if not st.saw_stop and STOP.exists():
                st.saw_stop = True
            if consume(CLEAR):
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
    released = outcome == "select" or archive(path)
    if not released:
        released = archive_failed(path)
    with st.lock:
        if released:
            st.claimed.discard(path.name)
        st.playing = None
        st.current_text = None
        st.current_voice = None
        st.current_piece = 0
        st.current_piece_count = 0
        if outcome == "skip":
            st.skip_name = path.name
    return True


def run_engine_loop() -> None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    SPOKEN.mkdir(parents=True, exist_ok=True)
    st = State()
    publish_status("loading", st, force=True)
    if not MODEL_PATH.exists() or not VOICES_PATH.exists():
        publish_status("setup_required", st, force=True)
        sys.stderr.write(f"missing kokoro files at {MODEL_DIR}\n")
        sys.exit(1)

    import sounddevice as sd
    import numpy as np
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
            if consume(INTERRUPT):
                log("INTERRUPT (idle); exiting")
                return
            process_play_request(buf, st)
            if consume(CLEAR):
                do_clear(buf, st)
            with st.lock:
                playing = st.playing
            if not playing and (st.saw_stop or consume(STOP)):
                log("STOP (idle); exiting")
                return
            if playing and not st.saw_stop and STOP.exists():
                st.saw_stop = True

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

            # Drop pieces invalidated while the worker was handing them off
            with st.lock:
                stale = st.skip_name == path.name or path.name not in st.claimed
                selected = not stale and first and st.selection_name == path.name
                if selected:
                    st.selection_name = None
                if first and st.skip_name and not stale:
                    st.skip_name = None
            if stale:
                continue

            if first and not session_first and not selected:
                g = gap_from_name(path.name)
                outcome = gap_wait(g if g is not None else CHUNK_GAP_S, buf, st)
                if outcome == "interrupt":
                    log("INTERRUPT (gap); exiting")
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
                if outcome == "skip":
                    finish_chunk_playback(path, "skip", True, st)
                    continue
            session_first = False

            if first:
                with st.lock:
                    st.playing = path.name
                    st.current_text = text
                    st.current_voice = voice
            with st.lock:
                st.current_piece = piece
                st.current_piece_count = piece_count
            if first:
                log(f"play {path.name}")
            publish_status("playing", st, force=True)
            outcome = play_one(sd, np, path, audio, sr,
                               "chunk" if first else "piece", buf, st)

            if finish_chunk_playback(path, outcome, last, st):
                publish_status("idle", st, force=True)

            if outcome == "interrupt":
                log("exiting on interrupt")
                return
            if st.saw_stop and (last or outcome == "skip"):
                consume(STOP)
                log("exiting on stop")
                return

    except KeyboardInterrupt:
        log("KeyboardInterrupt; exiting")
    finally:
        st.stop.set()
        publish_status("stopped", st, force=True)
        try:
            HEARTBEAT.unlink()
        except OSError:
            pass


def clear_transient_signals() -> None:
    for signal in (STOP, INTERRUPT, SKIP, CLEAR, PLAY, WARMUP):
        signal.unlink(missing_ok=True)
    for request in BASE.glob(f"{PLAY.stem}.*"):
        request.unlink(missing_ok=True)
    prune_play_acknowledgements()


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
    signal.touch()


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
    print(
        json.dumps(
            {
                "version": STATUS_VERSION,
                "state": "stopped",
                "updated_at": 0,
                "engine_pid": None,
                "current": None,
                "queue_count": len(list(QUEUE.glob("*.txt"))),
                "queue": [],
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
            print(enqueue_text(args.text, args.voice, args.gap_ms))
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
            request_id = request_play(args.chunk_id)
            print(json.dumps(wait_for_play_ack(request_id)))
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
