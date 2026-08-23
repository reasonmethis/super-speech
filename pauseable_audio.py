"""Sample-preserving pause support for sounddevice output streams."""

from __future__ import annotations

import threading
import time
from typing import Any


class PauseableAudio:
    """Feed an audio array to a sounddevice callback without losing position.

    The callback keeps the output stream alive with silence while paused. It
    advances the source position only when it writes real audio, so resume
    continues at the next sample instead of restarting the current chunk.
    """

    def __init__(self, audio: Any, callback_stop: type[BaseException]) -> None:
        if getattr(audio, "ndim", 1) == 1:
            audio = audio.reshape(-1, 1)
        if getattr(audio, "ndim", 0) != 2:
            raise ValueError("audio must be a one- or two-dimensional array")

        self.audio = audio
        self.callback_stop = callback_stop
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._paused = False
        self._position = 0
        self._pause_started: float | None = None
        self._paused_seconds = 0.0

    @property
    def channels(self) -> int:
        return int(self.audio.shape[1])

    @property
    def position(self) -> int:
        with self._lock:
            return self._position

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def paused_seconds(self) -> float:
        with self._lock:
            total = self._paused_seconds
            if self._pause_started is not None:
                total += time.monotonic() - self._pause_started
            return total

    def set_paused(self, paused: bool) -> bool:
        """Set the playback state and return whether it changed."""
        now = time.monotonic()
        with self._lock:
            if paused == self._paused:
                return False
            self._paused = paused
            if paused:
                self._pause_started = now
            elif self._pause_started is not None:
                self._paused_seconds += now - self._pause_started
                self._pause_started = None
            return True

    def callback(
        self,
        outdata: Any,
        frames: int,
        _time_info: Any,
        _status: Any,
    ) -> None:
        """Write the next frames, or silence while paused."""
        outdata.fill(0)
        with self._lock:
            if self._paused:
                return

            remaining = len(self.audio) - self._position
            frame_count = min(frames, remaining)
            if frame_count:
                end = self._position + frame_count
                outdata[:frame_count] = self.audio[self._position:end]
                self._position = end
            finished = self._position >= len(self.audio)

        if finished:
            raise self.callback_stop

    def mark_done(self) -> None:
        """Unblock the control loop when sounddevice closes the stream."""
        self.done.set()
