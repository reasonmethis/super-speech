#!/usr/bin/env python3
"""
Auto-podcast drainer. Plays *.txt chunk files from a queue directory in
lexicographic order, sequentially (no overlap). Uses the speech skill's
speak-eleven (ElevenLabs Sarah) for premium voice playback, falling back
to the system speak.cmd if speak-eleven isn't present.

Stops cleanly when a STOP file appears in BASE, or on KeyboardInterrupt.

Layout (auto-created):
    ~/.claude/podcast/queue/      pending NNN-slug.txt chunks
    ~/.claude/podcast/spoken/     archived after playback
    ~/.claude/podcast/log.txt     drainer log
    ~/.claude/podcast/STOP        touch to stop drainer cleanly
"""
import glob
import os
import subprocess
import sys
import time

BASE = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".claude", "podcast")
QUEUE = os.path.join(BASE, "queue")
SPOKEN = os.path.join(BASE, "spoken")
LOG = os.path.join(BASE, "log.txt")
STOP = os.path.join(BASE, "STOP")

BIN = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".claude", "bin")
SPEAK_PRIMARY = os.path.join(BIN, "speak-eleven.cmd")
SPEAK_FALLBACK = os.path.join(BIN, "speak.cmd")


def speak_cmd() -> str:
    if os.path.exists(SPEAK_PRIMARY):
        return SPEAK_PRIMARY
    if os.path.exists(SPEAK_FALLBACK):
        return SPEAK_FALLBACK
    sys.stderr.write(
        "neither speak-eleven.cmd nor speak.cmd found in ~/.claude/bin\n"
    )
    sys.exit(1)


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


def main() -> None:
    os.makedirs(QUEUE, exist_ok=True)
    os.makedirs(SPOKEN, exist_ok=True)
    speak = speak_cmd()
    log(f"drainer started; speak={os.path.basename(speak)}")
    while True:
        if os.path.exists(STOP):
            log("STOP file detected, exiting")
            try:
                os.remove(STOP)
            except OSError:
                pass
            return
        files = sorted(glob.glob(os.path.join(QUEUE, "*.txt")))
        if not files:
            time.sleep(2)
            continue
        f = files[0]
        name = os.path.basename(f)
        try:
            with open(f, "r", encoding="utf-8") as fp:
                text = fp.read().strip()
        except Exception as e:
            log(f"read error {name}: {e}")
            text = ""
        if text:
            log(f"speaking {name} ({len(text)} chars)")
            try:
                subprocess.run([speak, text], check=False)
            except Exception as e:
                log(f"speak error {name}: {e}")
        else:
            log(f"empty {name}, skipping")
        try:
            os.replace(f, os.path.join(SPOKEN, name))
        except Exception as e:
            log(f"archive error {name}: {e}")


if __name__ == "__main__":
    main()
