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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol

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
        with _com_apartment():
            enumerator = _create_device_enumerator()
            try:
                collection = _out_pointer(
                    lambda out: _call(enumerator, 3, _UINT, _UINT, _PVOID)(
                        _E_CAPTURE, _DEVICE_STATE_ACTIVE, out
                    )
                )
                try:
                    count = _UINT()
                    _call(collection, 3, _PVOID)(ctypes.byref(count))
                    device_ids: list[str] = []
                    for index in range(count.value):
                        device = _out_pointer(
                            lambda out: _call(collection, 4, _UINT, _PVOID)(index, out)
                        )
                        try:
                            if not _endpoint_muted(device):
                                device_ids.append(_device_id(device))
                        finally:
                            _release(device)
                    return device_ids
                finally:
                    _release(collection)
            finally:
                _release(enumerator)

    def set_muted(self, device_id: str, muted: bool) -> None:
        with _com_apartment():
            enumerator = _create_device_enumerator()
            try:
                device = _out_pointer(
                    lambda out: _call(enumerator, 5, ctypes.c_wchar_p, _PVOID)(
                        device_id, out
                    )
                )
                try:
                    volume = _activate_endpoint_volume(device)
                    try:
                        _call(volume, 14, ctypes.c_int, _PVOID)(int(muted), None)
                    finally:
                        _release(volume)
                finally:
                    _release(device)
            finally:
                _release(enumerator)


# Core Audio COM bindings. Python has no COM support without extra packages, so
# the few interface methods used here are called through their vtable slots.
# Slot numbers follow the interface declarations in mmdeviceapi.h and
# endpointvolume.h, where slots 0-2 are IUnknown.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _UINT = ctypes.c_uint
    _PVOID = ctypes.c_void_p
    _E_CAPTURE = 1
    _DEVICE_STATE_ACTIVE = 1
    _CLSCTX_ALL = 0x17
    _COINIT_MULTITHREADED = 0
    _RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 as a signed HRESULT

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        def __init__(self, text: str) -> None:
            super().__init__()
            ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(self))

    _CLSID_MM_DEVICE_ENUMERATOR = _GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    _IID_IMM_DEVICE_ENUMERATOR = _GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _IID_IAUDIO_ENDPOINT_VOLUME = _GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")

    _ole32 = ctypes.windll.ole32
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoInitializeEx.argtypes = [_PVOID, wintypes.DWORD]
    _ole32.CoTaskMemFree.argtypes = [_PVOID]

    def _call(pointer: int, slot: int, *argtypes: object) -> Callable[..., object]:
        """Bind one COM method; ctypes.HRESULT raises OSError on failure codes."""
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_PVOID))).contents
        prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, _PVOID, *argtypes)
        method = prototype(vtable[slot])
        return lambda *args: method(pointer, *args)

    def _out_pointer(invoke: Callable[[object], object]) -> int:
        result = _PVOID()
        invoke(ctypes.byref(result))
        if not result.value:
            raise OSError("Core Audio returned no object")
        return result.value

    def _release(pointer: int) -> None:
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_PVOID))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, _PVOID)(vtable[2])(pointer)

    @contextmanager
    def _com_apartment() -> Iterator[None]:
        result = _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        # A thread that already chose another COM model can still make these calls
        initialized = result >= 0
        if not initialized and result != _RPC_E_CHANGED_MODE:
            raise OSError(f"COM initialization failed: {result:#x}")
        try:
            yield
        finally:
            if initialized:
                _ole32.CoUninitialize()

    def _create_device_enumerator() -> int:
        return _out_pointer(
            lambda out: ctypes.oledll.ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MM_DEVICE_ENUMERATOR),
                None,
                _CLSCTX_ALL,
                ctypes.byref(_IID_IMM_DEVICE_ENUMERATOR),
                out,
            )
        )

    def _activate_endpoint_volume(device: int) -> int:
        return _out_pointer(
            lambda out: _call(device, 3, _PVOID, wintypes.DWORD, _PVOID, _PVOID)(
                ctypes.byref(_IID_IAUDIO_ENDPOINT_VOLUME), _CLSCTX_ALL, None, out
            )
        )

    def _endpoint_muted(device: int) -> bool:
        volume = _activate_endpoint_volume(device)
        try:
            muted = ctypes.c_int()
            _call(volume, 15, _PVOID)(ctypes.byref(muted))
            return bool(muted.value)
        finally:
            _release(volume)

    def _device_id(device: int) -> str:
        text = _out_pointer(lambda out: _call(device, 5, _PVOID)(out))
        try:
            return ctypes.wstring_at(text)
        finally:
            _ole32.CoTaskMemFree(text)
