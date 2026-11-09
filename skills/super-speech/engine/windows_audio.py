"""Call the Windows Core Audio API without extra packages.

Python has no COM support without extra packages, so the few interface methods
used here are called through their vtable slots. Slot numbers follow the
interface declarations in mmdeviceapi.h, endpointvolume.h, and audiopolicy.h,
where slots 0-2 are IUnknown. Import this module only on Windows.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from contextlib import contextmanager
from ctypes import wintypes
from typing import Iterator

UINT = ctypes.c_uint
PVOID = ctypes.c_void_p
E_RENDER = 0
E_CAPTURE = 1
DEVICE_STATE_ACTIVE = 1
CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 as a signed HRESULT


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(self))


_CLSID_MM_DEVICE_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMM_DEVICE_ENUMERATOR = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")

_ole32 = ctypes.windll.ole32
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = [PVOID, wintypes.DWORD]
_ole32.CoTaskMemFree.argtypes = [PVOID]


def call(pointer: int, slot: int, *argtypes: object) -> Callable[..., object]:
    """Bind one COM method; ctypes.HRESULT raises OSError on failure codes."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(PVOID))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, PVOID, *argtypes)
    method = prototype(vtable[slot])
    return lambda *args: method(pointer, *args)


def out_pointer(invoke: Callable[[object], object]) -> int:
    result = PVOID()
    invoke(ctypes.byref(result))
    if not result.value:
        raise OSError("Core Audio returned no object")
    return result.value


def release(pointer: int) -> None:
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(PVOID))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, PVOID)(vtable[2])(pointer)


def query_interface(pointer: int, iid: GUID) -> int:
    return out_pointer(lambda out: call(pointer, 0, PVOID, PVOID)(ctypes.byref(iid), out))


def free_string(text: int) -> str:
    """Read and free a string that Core Audio allocated."""
    try:
        return ctypes.wstring_at(text)
    finally:
        _ole32.CoTaskMemFree(text)


@contextmanager
def com_apartment() -> Iterator[None]:
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


def create_device_enumerator() -> int:
    return out_pointer(
        lambda out: ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MM_DEVICE_ENUMERATOR),
            None,
            CLSCTX_ALL,
            ctypes.byref(_IID_IMM_DEVICE_ENUMERATOR),
            out,
        )
    )


def active_devices(enumerator: int, flow: int) -> Iterator[int]:
    """Yield each active endpoint for `flow`, releasing it after the caller's turn."""
    collection = out_pointer(
        lambda out: call(enumerator, 3, UINT, UINT, PVOID)(
            flow, DEVICE_STATE_ACTIVE, out
        )
    )
    try:
        count = UINT()
        call(collection, 3, PVOID)(ctypes.byref(count))
        for index in range(count.value):
            device = out_pointer(
                lambda out: call(collection, 4, UINT, PVOID)(index, out)
            )
            try:
                yield device
            finally:
                release(device)
    finally:
        release(collection)


def activate(device: int, iid: GUID) -> int:
    """Create an interface such as endpoint volume or session manager on a device."""
    return out_pointer(
        lambda out: call(device, 3, PVOID, wintypes.DWORD, PVOID, PVOID)(
            ctypes.byref(iid), CLSCTX_ALL, None, out
        )
    )
