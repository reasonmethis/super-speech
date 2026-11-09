"""Notice when other apps play sound, so speech can wait for quiet.

Windows keeps a level meter for each audio session, which is one app's sound
on one output device. Reading those meters while skipping the engine's own
process separates other apps' sound from Super Speech's. Capturing the mixed
speaker output could not, because Super Speech is part of that mix.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Protocol

# Pauses between words and sentences are shorter, so they do not count as quiet
QUIET_S = 1.0
# One read takes a few milliseconds, so this costs a few percent of one core
POLL_INTERVAL_S = 0.1
# About -50 dBFS; a session below it counts as silent
SILENCE_PEAK = 0.003


class OtherAudio(Protocol):
    def playing(self) -> bool:
        """Return whether any other app is producing sound right now."""


class OtherAudioMonitor:
    """Remember when other apps last made sound, sampled on one thread.

    Sampling runs only while the setting is on, so a disabled setting costs a
    file check per sample.
    """

    def __init__(
        self,
        other_audio: OtherAudio,
        *,
        enabled: Callable[[], bool],
        log: Callable[[str], None],
        poll_interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._other_audio = other_audio
        self._enabled = enabled
        self._log = log
        self._poll_interval = poll_interval
        self._last_heard = -float("inf")
        self._read_failed = False
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="super-speech-other-audio",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def quiet_for(self) -> float:
        """Seconds since another app last made sound; infinite when never heard."""
        return time.monotonic() - self._last_heard

    def should_wait(self) -> bool:
        """Whether speech should wait because the setting is on and it is not quiet yet."""
        return self._enabled() and self.quiet_for() < QUIET_S

    def _run(self) -> None:
        while not self._stopping.wait(self._poll_interval):
            if not self._enabled():
                # Sound heard before the setting was turned off is no longer current
                self._last_heard = -float("inf")
                continue
            try:
                playing = self._other_audio.playing()
            except OSError as error:
                # A failed read counts as quiet, so a broken meter never blocks speech
                if not self._read_failed:
                    self._log(f"other audio check failed: {error}")
                self._read_failed = True
                continue
            self._read_failed = False
            if playing:
                self._last_heard = time.monotonic()


def system_other_audio(own_process_id: int) -> OtherAudio | None:
    """Return this platform's meter for other apps' sound, or None when unsupported."""
    if sys.platform == "win32":
        return WindowsOtherAudio(own_process_id)
    return None


class WindowsOtherAudio:
    """Read every active output device's per-app meters, except one process's."""

    def __init__(self, own_process_id: int) -> None:
        self._own_process_id = own_process_id
        self._com_threads = threading.local()

    def playing(self) -> bool:
        # Setting up COM costs most of a read, so each thread does it once and
        # keeps it; the monitor's thread lasts as long as the engine
        if not getattr(self._com_threads, "joined", False):
            windows_audio.join_com()
            self._com_threads.joined = True
        enumerator = windows_audio.create_device_enumerator()
        try:
            devices = windows_audio.active_devices(enumerator, windows_audio.E_RENDER)
            try:
                return any(self._device_playing(device) for device in devices)
            finally:
                devices.close()
        finally:
            windows_audio.release(enumerator)

    def _device_playing(self, device: int) -> bool:
        manager = windows_audio.activate(device, _IID_IAUDIO_SESSION_MANAGER2)
        try:
            sessions = windows_audio.out_pointer(
                lambda out: windows_audio.call(manager, 5, windows_audio.PVOID)(out)
            )
            try:
                count = ctypes.c_int()
                windows_audio.call(sessions, 3, windows_audio.PVOID)(ctypes.byref(count))
                for index in range(count.value):
                    session = windows_audio.out_pointer(
                        lambda out: windows_audio.call(
                            sessions, 4, ctypes.c_int, windows_audio.PVOID
                        )(index, out)
                    )
                    try:
                        if self._session_playing(session):
                            return True
                    finally:
                        windows_audio.release(session)
                return False
            finally:
                windows_audio.release(sessions)
        finally:
            windows_audio.release(manager)

    def _session_playing(self, session: int) -> bool:
        control = windows_audio.query_interface(session, _IID_IAUDIO_SESSION_CONTROL2)
        try:
            process_id = wintypes.DWORD()
            windows_audio.call(control, 14, windows_audio.PVOID)(ctypes.byref(process_id))
        finally:
            windows_audio.release(control)
        if process_id.value == self._own_process_id:
            return False
        meter = windows_audio.query_interface(session, _IID_IAUDIO_METER_INFORMATION)
        try:
            peak = ctypes.c_float()
            windows_audio.call(meter, 3, windows_audio.PVOID)(ctypes.byref(peak))
            return peak.value > SILENCE_PEAK
        finally:
            windows_audio.release(meter)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    import windows_audio

    _IID_IAUDIO_SESSION_MANAGER2 = windows_audio.GUID(
        "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
    )
    _IID_IAUDIO_SESSION_CONTROL2 = windows_audio.GUID(
        "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
    )
    _IID_IAUDIO_METER_INFORMATION = windows_audio.GUID(
        "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"
    )
