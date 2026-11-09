"""Mute microphones while Super Speech is audible, then restore them.

Only microphones that were unmuted when speech began are muted, so a
microphone the user muted stays muted afterward. Their device IDs are recorded
before muting, so an engine that restarts soon after a crash can unmute them.
The record is kept fresh while muted and ignored once stale, because by then
the user may have muted a microphone deliberately.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

RECORD_REFRESH_S = 5.0
# Long enough for the desktop app to restart a crashed or frozen engine
STALE_RECORD_S = 60.0


class CaptureDevices(Protocol):
    def unmuted_device_ids(self) -> list[str]: ...

    def set_muted(self, device_id: str, muted: bool) -> None: ...


class MicrophoneGuard:
    """Keep microphones muted while the engine is speaking.

    An `audibility.AudibilityWatcher` calls `begin`, then `update` on every
    sample, then `end`.
    """

    def __init__(
        self,
        devices: CaptureDevices,
        record: Path,
        *,
        enabled: Callable[[], bool],
        log: Callable[[str], None],
        record_refresh: float = RECORD_REFRESH_S,
    ) -> None:
        self._devices = devices
        self._record = record
        self._enabled = enabled
        self._log = log
        self._record_refresh = record_refresh
        self._muted: list[str] | None = None
        self._last_refresh = 0.0

    def begin(self) -> None:
        restore_microphones(
            self._devices, self._record, self._log, max_age=STALE_RECORD_S
        )

    def update(self, speaking: bool, _last_sound: float) -> None:
        # The setting is read only while speaking, so idle sampling stays cheap
        want_muted = speaking and self._enabled()
        now = time.monotonic()
        if want_muted and self._muted is None:
            self._muted = self._mute()
            self._last_refresh = now
        elif not want_muted and self._muted is not None:
            restore_microphones(self._devices, self._record, self._log)
            self._muted = None
        elif self._muted and now - self._last_refresh >= self._record_refresh:
            self._last_refresh = now
            try:
                os.utime(self._record)
            except OSError as error:
                self._log(f"microphone mute record refresh failed: {error}")

    def end(self) -> None:
        if self._muted is not None:
            restore_microphones(self._devices, self._record, self._log)
            self._muted = None

    def _mute(self) -> list[str]:
        try:
            device_ids = self._devices.unmuted_device_ids()
        except OSError as error:
            self._log(f"microphone mute skipped: {error}")
            return []
        if not device_ids:
            return []
        try:
            _write_record(self._record, device_ids)
        except OSError as error:
            # Without the record a killed engine could leave microphones muted
            self._log(f"microphone mute skipped: {error}")
            return []
        for device_id in device_ids:
            try:
                self._devices.set_muted(device_id, True)
            except OSError as error:
                self._log(f"microphone mute failed for {device_id}: {error}")
        return device_ids


def restore_microphones(
    devices: CaptureDevices,
    record: Path,
    log: Callable[[str], None],
    *,
    max_age: float | None = None,
) -> None:
    """Unmute every microphone listed in the record, then remove the record.

    A record older than `max_age` seconds is removed without unmuting anything.
    """
    try:
        if max_age is not None and time.time() - record.stat().st_mtime > max_age:
            log("ignored a stale microphone mute record")
            record.unlink(missing_ok=True)
            return
        device_ids = json.loads(record.read_text(encoding="utf-8"))["devices"]
        if not isinstance(device_ids, list) or not all(
            isinstance(device_id, str) for device_id in device_ids
        ):
            raise ValueError("device list is malformed")
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError) as error:
        log(f"microphone mute record is unreadable: {error}")
        record.unlink(missing_ok=True)
        return
    for device_id in device_ids:
        try:
            devices.set_muted(device_id, False)
        except OSError as error:
            # An unplugged microphone cannot be restored and must not block the rest
            log(f"microphone unmute failed for {device_id}: {error}")
    record.unlink(missing_ok=True)


def _write_record(record: Path, device_ids: list[str]) -> None:
    temporary = record.with_name(f"{record.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"devices": device_ids}), encoding="utf-8")
    os.replace(temporary, record)


def system_capture_devices() -> CaptureDevices | None:
    """Return this platform's microphone controls, or None when unsupported."""
    if sys.platform == "win32":
        return WindowsCaptureDevices()
    return None


class WindowsCaptureDevices:
    """Mute active recording endpoints through the Windows Core Audio API."""

    def unmuted_device_ids(self) -> list[str]:
        with windows_audio.com_apartment():
            enumerator = windows_audio.create_device_enumerator()
            try:
                return [
                    _device_id(device)
                    for device in windows_audio.active_devices(
                        enumerator, windows_audio.E_CAPTURE
                    )
                    if not _endpoint_muted(device)
                ]
            finally:
                windows_audio.release(enumerator)

    def set_muted(self, device_id: str, muted: bool) -> None:
        with windows_audio.com_apartment():
            enumerator = windows_audio.create_device_enumerator()
            try:
                device = windows_audio.out_pointer(
                    lambda out: windows_audio.call(
                        enumerator, 5, ctypes.c_wchar_p, windows_audio.PVOID
                    )(device_id, out)
                )
                try:
                    volume = windows_audio.activate(device, _IID_IAUDIO_ENDPOINT_VOLUME)
                    try:
                        windows_audio.call(volume, 14, ctypes.c_int, windows_audio.PVOID)(
                            int(muted), None
                        )
                    finally:
                        windows_audio.release(volume)
                finally:
                    windows_audio.release(device)
            finally:
                windows_audio.release(enumerator)


if sys.platform == "win32":
    import ctypes

    import windows_audio

    _IID_IAUDIO_ENDPOINT_VOLUME = windows_audio.GUID(
        "{5CDF2C82-841E-4546-9722-0CF74078229A}"
    )

    def _endpoint_muted(device: int) -> bool:
        volume = windows_audio.activate(device, _IID_IAUDIO_ENDPOINT_VOLUME)
        try:
            muted = ctypes.c_int()
            windows_audio.call(volume, 15, windows_audio.PVOID)(ctypes.byref(muted))
            return bool(muted.value)
        finally:
            windows_audio.release(volume)

    def _device_id(device: int) -> str:
        return windows_audio.free_string(
            windows_audio.out_pointer(
                lambda out: windows_audio.call(device, 5, windows_audio.PVOID)(out)
            )
        )
