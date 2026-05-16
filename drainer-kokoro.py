#!/usr/bin/env python3
"""
Auto-podcast drainer (Kokoro, single-thread, non-blocking audio).

One main loop. Playback runs through the OS audio driver via sounddevice
(non-blocking), so while a chunk plays the same thread can synthesize the
next chunk. The OS provides the audio parallelism; no second Python thread.

Signal files in BASE control runtime (consumed when handled):
  STOP       - finish current chunk, then exit cleanly
  INTERRUPT  - stop playback immediately and exit
  SKIP       - stop current chunk, archive it, continue with next
  CLEAR      - move every non-playing queued chunk to spoken/
"""
import glob
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".claude" / "podcast"
QUEUE = BASE / "queue"
SPOKEN = BASE / "spoken"
FAILED = BASE / "failed"
LOG = BASE / "log.txt"

STOP = BASE / "STOP"
INTERRUPT = BASE / "INTERRUPT"
SKIP = BASE / "SKIP"
CLEAR = BASE / "CLEAR"

TONE_WAV = BASE / "tone.wav"

MODEL_DIR = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".claude" / "models" / "kokoro"
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

DEFAULT_VOICE = "af_bella"
AVAILABLE_VOICES: set[str] = set()  # populated in main() once kokoro loads
POLL_INTERVAL = 0.2   # idle poll cadence
SIGNAL_TICK = 0.05    # signal-file check cadence during playback
CHUNK_GAP_S = 1.0     # silence inserted after each chunk for natural pacing
PLAY_TONE = False     # short tone when synth starts; False mutes but keeps code path

TONE_FREQ_HZ = 880    # short "synth starting" blip
TONE_DUR_S = 0.08
TONE_SR = 22050
TONE_VOLUME = 0.25


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


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
    except OSError as e:
        log(f"archive error {path.name}: {e}")


def archive_failed(path: Path) -> None:
    """Move a chunk that couldn't be synthesized into failed/ so the queue doesn't loop on it."""
    try:
        FAILED.mkdir(parents=True, exist_ok=True)
        os.replace(str(path), str(FAILED / path.name))
    except OSError as e:
        log(f"archive_failed error {path.name}: {e}")


def clear_queue(currently_playing: Path | None) -> int:
    keep = None if currently_playing is None else currently_playing.name
    n = 0
    for f in glob.glob(str(QUEUE / "*.txt")):
        if keep is not None and os.path.basename(f) == keep:
            continue
        try:
            os.replace(f, str(SPOKEN / os.path.basename(f)))
            n += 1
        except OSError:
            pass
    return n


def init_tone() -> None:
    """Generate the synth-start tone WAV. Played later via winsound (async),
    which goes through a separate Windows audio path and therefore mixes
    with sounddevice playback at the OS level instead of cutting it off."""
    import numpy as np
    import wave
    n = int(TONE_DUR_S * TONE_SR)
    t = np.arange(n, dtype=np.float32) / TONE_SR
    tone = (TONE_VOLUME * np.sin(2 * np.pi * TONE_FREQ_HZ * t)).astype(np.float32)
    fade = max(1, int(0.01 * TONE_SR))
    env = np.ones(n, dtype=np.float32)
    env[:fade] = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    env[-fade:] = np.linspace(1.0, 0.0, fade, dtype=np.float32)
    tone *= env
    pcm = (np.clip(tone, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    TONE_WAV.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(TONE_WAV), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TONE_SR)
        w.writeframes(pcm)


def play_tone() -> None:
    """Fire-and-forget tone via winsound (async). Mixes with sounddevice.
    No-op when PLAY_TONE is False; code preserved for future re-enable."""
    if not PLAY_TONE:
        return
    import winsound
    flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
    try:
        winsound.PlaySound(str(TONE_WAV), flags)
    except Exception as e:
        log(f"tone error: {e}")


def stream_active(sd) -> bool:
    try:
        s = sd.get_stream()
    except Exception:
        return False
    return s is not None and getattr(s, "active", False)


def synth_chunk(kokoro, path: Path):
    """Return (audio, sr) or None on empty / read error / synth error."""
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


def wait_for_chunk() -> Path | None:
    """Block until a chunk appears or STOP/INTERRUPT is seen. Returns path or None to exit."""
    while True:
        if consume(STOP):
            log("STOP (idle); exiting")
            return None
        if consume(INTERRUPT):
            log("INTERRUPT (idle); exiting")
            return None
        if consume(CLEAR):
            log("CLEAR (idle); no-op")
        files = sorted(glob.glob(str(QUEUE / "*.txt")))
        if files:
            return Path(files[0])
        time.sleep(POLL_INTERVAL)


def find_next(after: Path) -> Path | None:
    for f in sorted(glob.glob(str(QUEUE / "*.txt"))):
        if os.path.basename(f) != after.name:
            return Path(f)
    return None


def play_and_overlap_synth(sd, kokoro, current_path: Path, audio, sr, next_path: Path | None):
    """
    Start non-blocking playback. While it plays, synthesize next_path (if any).
    Then poll for completion or a signal.

    Returns (outcome, next_pending):
      outcome      : "done" | "stop" | "interrupt" | "skip"
      next_pending : None or (path, audio, sr)
    """
    duration_s = float(len(audio)) / float(sr)
    sd.play(audio, sr)

    next_pending = None
    if next_path is not None:
        play_tone()
        res = synth_chunk(kokoro, next_path)
        if res is not None:
            next_pending = (next_path, res[0], res[1])

    deadline = time.time() + duration_s + 2.0  # safety guard
    outcome = "done"
    saw_stop = STOP.exists()

    while True:
        if not stream_active(sd):
            break
        if consume(INTERRUPT):
            sd.stop()
            outcome = "interrupt"
            break
        if consume(SKIP):
            sd.stop()
            outcome = "skip"
            break
        if not saw_stop and STOP.exists():
            saw_stop = True
        if consume(CLEAR):
            n = clear_queue(currently_playing=current_path)
            log(f"CLEAR; dropped {n} queued chunk(s)")
            next_pending = None
        if time.time() >= deadline:
            break
        time.sleep(SIGNAL_TICK)

    try:
        sd.wait()
    except Exception:
        pass

    gap_s = CHUNK_GAP_S
    if next_pending is not None:
        specified = gap_from_name(next_pending[0].name)
        if specified is not None:
            gap_s = specified

    if outcome == "done" and gap_s > 0:
        gap_deadline = time.time() + gap_s
        while time.time() < gap_deadline:
            if consume(INTERRUPT):
                outcome = "interrupt"
                break
            if consume(SKIP):
                outcome = "skip"
                break
            if not saw_stop and STOP.exists():
                saw_stop = True
            if consume(CLEAR):
                n = clear_queue(currently_playing=None)
                log(f"CLEAR (gap); dropped {n} queued chunk(s)")
                next_pending = None
            time.sleep(SIGNAL_TICK)

    if outcome == "done" and saw_stop and consume(STOP):
        outcome = "stop"
    return outcome, next_pending


def main() -> None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    SPOKEN.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists() or not VOICES_PATH.exists():
        sys.stderr.write(f"missing kokoro files at {MODEL_DIR}\n")
        sys.exit(1)

    import sounddevice as sd
    import numpy as np
    init_tone()
    # Warm up PortAudio so the first real sd.play() doesn't pay device-open
    # latency (otherwise the first chunk starts ~1s late and any winsound
    # tone fired right after sd.play() audibly precedes the chunk).
    sd.play(np.zeros(int(0.1 * 24000), dtype=np.float32), 24000)
    sd.wait()
    log("loading kokoro model...")
    from kokoro_onnx import Kokoro
    kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    global AVAILABLE_VOICES
    try:
        AVAILABLE_VOICES = set(kokoro.get_voices())
    except Exception as e:
        log(f"could not enumerate voices: {e}; voice validation disabled")
        AVAILABLE_VOICES = set()
    log(
        f"kokoro loaded ({len(AVAILABLE_VOICES)} voices); "
        "single-thread drainer (signals: STOP/INTERRUPT/SKIP/CLEAR)"
    )

    pending = None  # (path, audio, sr) — pre-synth waiting to play

    try:
        while True:
            if consume(CLEAR):
                n = clear_queue(currently_playing=None)
                log(f"CLEAR (idle); dropped {n} queued chunk(s)")
                pending = None

            tone_t0 = None
            if pending is not None:
                path, audio, sr = pending
                pending = None
            else:
                path = wait_for_chunk()
                if path is None:
                    return
                play_tone()
                tone_t0 = time.time()
                res = synth_chunk(kokoro, path)
                if res is None:
                    continue
                audio, sr = res

            log(f"play {path.name}")
            if tone_t0 is not None:
                log(f"tone-to-audio gap: {time.time() - tone_t0:.2f}s")
            next_path = find_next(after=path)
            outcome, next_pending = play_and_overlap_synth(
                sd, kokoro, path, audio, sr, next_path
            )
            archive(path)

            if outcome in ("interrupt", "stop"):
                log(f"exiting on {outcome}")
                return
            pending = next_pending

    except KeyboardInterrupt:
        log("KeyboardInterrupt; exiting")


if __name__ == "__main__":
    main()
