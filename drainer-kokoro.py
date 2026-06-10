#!/usr/bin/env python3
"""
Auto-podcast drainer (Kokoro) — background-worker streaming buffer.

A background synth WORKER thread synthesizes queued chunks continuously and
banks the rendered audio in a bounded buffer. The MAIN thread consumes that
buffer, playing each chunk through the OS audio driver via sounddevice
(non-blocking) and staying responsive to control signals every ~50 ms.

Because synth runs faster than playback, the worker races ahead and the buffer
fills during long chunks. That cushion is what makes SHORT chunks safe anywhere
in a script: by the time a brief chunk plays, the next (possibly long) chunk is
already rendered and waiting — so there is no synth-overrun gap. The earlier
single-thread design only ever rendered ONE chunk ahead, so a short chunk before
a long one always opened an audible gap; this removes that constraint.

Signal files in BASE control runtime (consumed when handled):
  STOP       - finish the current chunk, then exit cleanly
  INTERRUPT  - stop playback immediately and exit
  SKIP       - stop current chunk, archive it, continue with next
  CLEAR      - drop the buffer and every non-playing queued chunk (-> spoken/)
  WARMUP     - synthesize a throwaway phrase to pay the first-inference cost

Env:
  SUPER_SPEECH_HOME    - override the runtime home directory
  SUPER_SPEECH_SILENT  - opt-in: play silence of identical duration instead of
                         audio (timing is preserved). For measuring gap behavior
                         without making sound; default off.
"""
import glob
import os
import queue
import sys
import threading
import time
from pathlib import Path

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
INTERRUPT = BASE / "INTERRUPT"
SKIP = BASE / "SKIP"
CLEAR = BASE / "CLEAR"
WARMUP = BASE / "WARMUP"
HEARTBEAT = BASE / "drainer.alive"  # touched while alive; ensure-drainer.sh reads its mtime

MODEL_DIR = BASE / "models" / "kokoro"
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

DEFAULT_VOICE = "af_bella"
AVAILABLE_VOICES: set[str] = set()  # populated in main() once kokoro loads
POLL_INTERVAL = 0.2   # idle poll cadence
SIGNAL_TICK = 0.05    # signal-file check cadence during playback
CHUNK_GAP_S = 0.2     # silence before each chunk (natural rhythm); override per-file with -gMMM-
BUFFER_MAX = 8        # chunks of pre-rendered audio the worker may bank ahead (~4 is enough)

SILENT = bool(os.environ.get("SUPER_SPEECH_SILENT"))


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
    """Touch the liveness file so ensure-drainer.sh can detect us cheaply.
    Throttled to ~1/s; force=True before a long blocking synth keeps it fresh."""
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


def consume(signal: Path) -> bool:
    if not signal.exists():
        return False
    try:
        signal.unlink()
    except OSError:
        pass
    return True


def archive(path: Path) -> None:
    try:
        os.replace(str(path), str(SPOKEN / path.name))
    except OSError:
        pass  # already moved (e.g. by CLEAR) — harmless


def archive_failed(path: Path) -> None:
    """Move a chunk that couldn't be synthesized into failed/ so the queue doesn't loop on it."""
    try:
        FAILED.mkdir(parents=True, exist_ok=True)
        os.replace(str(path), str(FAILED / path.name))
    except OSError as e:
        log(f"archive_failed error {path.name}: {e}")


def stream_active(sd) -> bool:
    try:
        s = sd.get_stream()
    except Exception:
        return False
    return s is not None and getattr(s, "active", False)


def synth_chunk(kokoro, path: Path):
    """Return (audio, sr) or None on empty / read error / synth error."""
    heartbeat(force=True)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        log(f"read error {path.name}: {e}")
        return None
    if not text:
        log(f"empty {path.name}, archiving")
        archive(path)
        return None
    voice = voice_from_name(path.name)
    t0 = time.time()
    try:
        audio, sr = kokoro.create(text, voice=voice, speed=1.0, lang="en-us")
    except Exception as e:
        log(f"synth error {path.name} (voice={voice}): {e}; moving to failed/")
        archive_failed(path)
        return None
    log(
        f"synth {path.name} voice={voice} chars={len(text)} "
        f"synth={time.time()-t0:.1f}s audio={len(audio)/sr:.1f}s"
    )
    return audio, sr


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
        self.stop = threading.Event()    # tell the worker to exit
        self.saw_stop = False            # latched STOP — finish current chunk, then exit


def drop_to_spoken(path: Path) -> None:
    try:
        os.replace(str(path), str(SPOKEN / path.name))
    except OSError:
        pass


def do_clear(buf: "queue.Queue", st: State) -> None:
    """Drop the rendered buffer and every non-playing queued chunk into spoken/."""
    with st.lock:
        n = 0
        while True:
            try:
                p, _a, _sr = buf.get_nowait()
            except queue.Empty:
                break
            drop_to_spoken(p)
            n += 1
        for f in glob.glob(str(QUEUE / "*.txt")):
            if os.path.basename(f) == st.playing:
                continue
            drop_to_spoken(Path(f))
            n += 1
        st.claimed = {st.playing} if st.playing else set()
    log(f"CLEAR; dropped {n} buffered/queued chunk(s)")


def synth_worker(kokoro, buf: "queue.Queue", st: State) -> None:
    """Producer: continuously claim the next queued chunk, synthesize it, and
    bank the audio in buf (blocking when buf is full — natural backpressure)."""
    while not st.stop.is_set():
        if consume(WARMUP):
            warmup(kokoro)
        nxt: Path | None = None
        with st.lock:
            for f in sorted(glob.glob(str(QUEUE / "*.txt"))):
                name = os.path.basename(f)
                if name in st.claimed or name == st.playing:
                    continue
                nxt = Path(f)
                st.claimed.add(name)
                break
        if nxt is None:
            heartbeat()
            time.sleep(POLL_INTERVAL)
            continue
        res = synth_chunk(kokoro, nxt)
        if res is None:
            with st.lock:
                st.claimed.discard(nxt.name)
            continue
        audio, sr = res
        while not st.stop.is_set():
            try:
                buf.put((nxt, audio, sr), timeout=0.2)
                break
            except queue.Full:
                heartbeat()


def gap_wait(seconds: float, buf: "queue.Queue", st: State) -> str | None:
    """Sleep for `seconds` while honoring control signals. Returns
    'interrupt'/'skip' if one fired, else None."""
    end = time.time() + seconds
    while time.time() < end:
        if consume(INTERRUPT):
            return "interrupt"
        if consume(SKIP):
            return "skip"
        if not st.saw_stop and STOP.exists():
            st.saw_stop = True
        if consume(CLEAR):
            do_clear(buf, st)
        time.sleep(SIGNAL_TICK)
    return None


def play_one(sd, np, path: Path, audio, sr, buf: "queue.Queue", st: State) -> str:
    """Play a pre-rendered chunk (non-blocking) and poll for completion/signals.
    Returns 'done' | 'interrupt' | 'skip'."""
    out = np.zeros(len(audio), dtype=getattr(audio, "dtype", "float32")) if SILENT else audio
    t0 = time.time()
    sd.play(out, sr)
    deadline = time.time() + (len(audio) / float(sr)) + 2.0  # safety guard
    while True:
        if not stream_active(sd):
            break
        if consume(INTERRUPT):
            sd.stop()
            return "interrupt"
        if consume(SKIP):
            sd.stop()
            return "skip"
        if not st.saw_stop and STOP.exists():
            st.saw_stop = True
        if consume(CLEAR):
            do_clear(buf, st)
        if time.time() >= deadline:
            break
        heartbeat()
        time.sleep(SIGNAL_TICK)
    try:
        sd.wait()
    except Exception:
        pass
    # Wall clock stretching past the audio duration means the device starved
    # mid-chunk (buffer underruns -> audible stutter), so record it.
    wall = time.time() - t0
    dur = len(audio) / float(sr)
    lag = wall - dur
    log(
        f"played {path.name} wall={wall:.1f}s audio={dur:.1f}s"
        + (f" UNDERRUN +{lag:.1f}s" if lag > 0.5 else "")
    )
    return "done"


def main() -> None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    SPOKEN.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists() or not VOICES_PATH.exists():
        sys.stderr.write(f"missing kokoro files at {MODEL_DIR}\n")
        sys.exit(1)

    import sounddevice as sd
    import numpy as np
    # Warm up PortAudio so the first real sd.play() doesn't pay device-open latency.
    sd.play(np.zeros(int(0.1 * 24000), dtype=np.float32), 24000)
    sd.wait()
    log("loading kokoro model...")
    from kokoro_onnx import Kokoro
    # Cap ONNX intra-op threads below the core count so a synth burst (worst
    # right after a cold load) can never starve the audio callback into stutter.
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
        f"(buffer<= {BUFFER_MAX}, gap={CHUNK_GAP_S}s{', SILENT' if SILENT else ''})"
    )

    # Pay the one-time first-inference cost now (before the worker starts, so
    # there is no concurrent kokoro call), then clear any pre-launch WARMUP.
    warmup(kokoro)
    consume(WARMUP)

    buf: "queue.Queue" = queue.Queue(maxsize=BUFFER_MAX)
    st = State()
    worker = threading.Thread(target=synth_worker, args=(kokoro, buf, st), daemon=True)
    worker.start()

    first = True
    try:
        while True:
            if consume(INTERRUPT):
                log("INTERRUPT (idle); exiting")
                return
            if consume(CLEAR):
                do_clear(buf, st)
            if not st.saw_stop and STOP.exists():
                st.saw_stop = True

            try:
                path, audio, sr = buf.get(timeout=POLL_INTERVAL)
            except queue.Empty:
                heartbeat()
                # STOP/idle: nothing playing and nothing buffered -> exit cleanly.
                if (st.saw_stop or consume(STOP)) and buf.empty():
                    with st.lock:
                        more = bool(glob.glob(str(QUEUE / "*.txt")))
                    if not more or st.saw_stop:
                        log("STOP (idle); exiting")
                        return
                continue

            if not first:
                g = gap_from_name(path.name)
                outcome = gap_wait(g if g is not None else CHUNK_GAP_S, buf, st)
                if outcome == "interrupt":
                    log("INTERRUPT (gap); exiting")
                    return
                if outcome == "skip":
                    archive(path)
                    with st.lock:
                        st.claimed.discard(path.name)
                    continue
            first = False

            with st.lock:
                st.playing = path.name
            log(f"play {path.name}")
            outcome = play_one(sd, np, path, audio, sr, buf, st)
            archive(path)
            with st.lock:
                st.claimed.discard(path.name)
                st.playing = None

            if outcome == "interrupt":
                log("exiting on interrupt")
                return
            if st.saw_stop and consume(STOP) or st.saw_stop:
                log("exiting on stop")
                return

    except KeyboardInterrupt:
        log("KeyboardInterrupt; exiting")
    finally:
        st.stop.set()
        try:
            HEARTBEAT.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
