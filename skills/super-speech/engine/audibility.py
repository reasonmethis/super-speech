"""Act on whether Super Speech is audible, sampled on one thread.

The engine speaks while a live stream is allowed to produce sound. Audio stops
briefly between sentences and between replies, so a gap shorter than
`RELEASE_DELAY_S` still counts as speaking; that keeps a microphone mute from
flickering and keeps one reply as one published stretch.

Published stretches (`AudibleSpanFile`) are a contract with other local apps,
currently WhisperPilot, which erases them from its dictation while keeping
other speaker sound, such as a caller's voice. The file holds JSON:

    {"version": 1, "updated": <time>, "spans": [[<start>, <end or null>], ...]}

Times are wall-clock seconds (`time.time()`), because monotonic clocks are not
guaranteed to match across processes and Python versions. An end is null while
speech continues, and the file is rewritten at least every `SPAN_REFRESH_S`
then, so a reader treats an open span whose "updated" time is a few seconds old
as ended at that time. Spans stay for `SPAN_KEEP_S`, longer than the longest
recording a reader judges.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

RELEASE_DELAY_S = 0.3
POLL_INTERVAL_S = 0.02
SPAN_FORMAT_VERSION = 1
SPAN_REFRESH_S = 0.5
# WhisperPilot keeps a take for up to an hour
SPAN_KEEP_S = 65 * 60.0


class AudibilityListener(Protocol):
    def begin(self) -> None: ...

    def update(self, speaking: bool, last_sound: float) -> None:
        """Receive one sample; `last_sound` is the monotonic time sound was last allowed."""

    def end(self) -> None: ...


class AudibilityWatcher:
    """Sample audibility on one thread and pass each sample to every listener."""

    def __init__(
        self,
        audible: Callable[[], bool],
        listeners: Sequence[AudibilityListener],
        *,
        release_delay: float = RELEASE_DELAY_S,
        poll_interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._audible = audible
        self._listeners = listeners
        self._release_delay = release_delay
        self._poll_interval = poll_interval
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="super-speech-audibility",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        for listener in self._listeners:
            listener.begin()
        last_sound = -float("inf")
        try:
            while not self._stopping.wait(self._poll_interval):
                now = time.monotonic()
                if self._audible():
                    last_sound = now
                speaking = now - last_sound < self._release_delay
                for listener in self._listeners:
                    listener.update(speaking, last_sound)
        finally:
            for listener in self._listeners:
                listener.end()


class AudibleSpanFile:
    """Publish the stretches when Super Speech was audible (see module docstring)."""

    def __init__(
        self,
        path: Path,
        log: Callable[[str], None],
        *,
        refresh: float = SPAN_REFRESH_S,
    ) -> None:
        self._path = path
        self._log = log
        self._refresh = refresh
        self._spans: list[list[float | None]] = []
        self._last_sound = 0.0
        self._last_write = 0.0
        self._dirty = False
        self._write_failed = False

    def begin(self) -> None:
        # A previous engine's open span would otherwise read as speech until it went stale
        self._path.unlink(missing_ok=True)

    def update(self, speaking: bool, last_sound: float) -> None:
        self._last_sound = last_sound
        is_open = bool(self._spans) and self._spans[-1][1] is None
        now = time.monotonic()
        if speaking and not is_open:
            self._spans.append([_wall_time(last_sound), None])
            self._dirty = True
        elif not speaking and is_open:
            self._spans[-1][1] = _wall_time(last_sound)
            self._dirty = True
        elif is_open and now - self._last_write >= self._refresh:
            self._dirty = True
        if self._dirty:
            self._write(now)

    def end(self) -> None:
        if self._spans and self._spans[-1][1] is None:
            self._spans[-1][1] = _wall_time(self._last_sound)
            self._write(time.monotonic())

    def _write(self, now: float) -> None:
        horizon = time.time() - SPAN_KEEP_S
        self._spans = [
            span for span in self._spans if span[1] is None or span[1] >= horizon
        ]
        payload = {
            "version": SPAN_FORMAT_VERSION,
            "updated": time.time(),
            "spans": self._spans,
        }
        temporary = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as error:
            # A reader holding the file blocks replacement on Windows; the next sample retries
            if not self._write_failed:
                self._log(f"audible span publication failed: {error}")
            self._write_failed = True
            return
        self._dirty = False
        self._write_failed = False
        self._last_write = now


def _wall_time(monotonic_time: float) -> float:
    return time.time() - (time.monotonic() - monotonic_time)
