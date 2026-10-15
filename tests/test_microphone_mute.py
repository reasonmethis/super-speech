from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from audibility import AudibilityWatcher
from engine_control import LivePlaybackControl
from engine_test_support import CallbackStop, configure_runtime, load_engine, set_current
from microphone_mute import MicrophoneGuard, restore_microphones


class FakeDevices:
    def __init__(self, muted: dict[str, bool]) -> None:
        self.muted = muted
        self.changes: list[tuple[str, bool]] = []

    def unmuted_device_ids(self) -> list[str]:
        return [device_id for device_id, muted in self.muted.items() if not muted]

    def set_muted(self, device_id: str, muted: bool) -> None:
        if device_id not in self.muted:
            raise OSError("device is gone")
        self.muted[device_id] = muted
        self.changes.append((device_id, muted))


class Switch:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def __call__(self) -> bool:
        return self.value


def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        time.sleep(0.005)


def start_guard(
    devices: FakeDevices,
    record: Path,
    *,
    enabled: Switch,
    audible: Switch,
    release_delay: float = 0.05,
    record_refresh: float = 5.0,
) -> AudibilityWatcher:
    guard = MicrophoneGuard(
        devices,
        record,
        enabled=enabled,
        log=lambda _message: None,
        record_refresh=record_refresh,
    )
    watcher = AudibilityWatcher(
        audible, [guard], release_delay=release_delay, poll_interval=0.005
    )
    watcher.start()
    return watcher


def test_guard_mutes_only_unmuted_microphones_while_audible(tmp_path: Path) -> None:
    record = tmp_path / "microphone-mute.json"
    devices = FakeDevices({"headset": False, "webcam": True})
    audible = Switch()
    guard = start_guard(devices, record, enabled=Switch(True), audible=audible)
    try:
        audible.value = True
        wait_until(lambda: devices.muted["headset"])
        assert json.loads(record.read_text(encoding="utf-8")) == {"devices": ["headset"]}

        audible.value = False
        wait_until(lambda: not devices.muted["headset"])
        assert devices.muted["webcam"] is True
        assert not record.exists()
    finally:
        guard.stop()


def test_guard_holds_mute_across_short_gaps(tmp_path: Path) -> None:
    devices = FakeDevices({"headset": False})
    audible = Switch(True)
    guard = start_guard(
        devices,
        tmp_path / "microphone-mute.json",
        enabled=Switch(True),
        audible=audible,
        release_delay=0.5,
    )
    try:
        wait_until(lambda: devices.muted["headset"])
        audible.value = False
        time.sleep(0.1)
        audible.value = True
        time.sleep(0.1)
        assert devices.changes == [("headset", True)]
    finally:
        guard.stop()


def test_guard_leaves_microphones_alone_when_disabled(tmp_path: Path) -> None:
    devices = FakeDevices({"headset": False})
    guard = start_guard(
        devices,
        tmp_path / "microphone-mute.json",
        enabled=Switch(False),
        audible=Switch(True),
    )
    time.sleep(0.05)
    guard.stop()
    assert devices.changes == []


def test_disabling_the_setting_unmutes_during_speech(tmp_path: Path) -> None:
    devices = FakeDevices({"headset": False})
    enabled = Switch(True)
    guard = start_guard(
        devices,
        tmp_path / "microphone-mute.json",
        enabled=enabled,
        audible=Switch(True),
    )
    try:
        wait_until(lambda: devices.muted["headset"])
        enabled.value = False
        wait_until(lambda: not devices.muted["headset"])
    finally:
        guard.stop()


def test_stopping_the_guard_restores_muted_microphones(tmp_path: Path) -> None:
    record = tmp_path / "microphone-mute.json"
    devices = FakeDevices({"headset": False})
    guard = start_guard(devices, record, enabled=Switch(True), audible=Switch(True))
    wait_until(lambda: devices.muted["headset"])

    guard.stop()

    assert devices.muted["headset"] is False
    assert not record.exists()


def test_guard_restores_microphones_left_by_a_killed_engine(tmp_path: Path) -> None:
    record = tmp_path / "microphone-mute.json"
    record.write_text(json.dumps({"devices": ["headset"]}), encoding="utf-8")
    devices = FakeDevices({"headset": True})
    guard = start_guard(devices, record, enabled=Switch(True), audible=Switch())
    try:
        wait_until(lambda: not record.exists())
        assert devices.muted["headset"] is False
    finally:
        guard.stop()


def test_guard_ignores_a_stale_record_from_a_long_dead_engine(tmp_path: Path) -> None:
    record = tmp_path / "microphone-mute.json"
    record.write_text(json.dumps({"devices": ["headset"]}), encoding="utf-8")
    two_minutes_ago = time.time() - 120
    os.utime(record, (two_minutes_ago, two_minutes_ago))
    devices = FakeDevices({"headset": True})
    guard = start_guard(devices, record, enabled=Switch(True), audible=Switch())
    try:
        wait_until(lambda: not record.exists())
        assert devices.muted["headset"] is True
        assert devices.changes == []
    finally:
        guard.stop()


def test_guard_keeps_its_record_fresh_while_muted(tmp_path: Path) -> None:
    record = tmp_path / "microphone-mute.json"
    devices = FakeDevices({"headset": False})
    guard = start_guard(
        devices,
        record,
        enabled=Switch(True),
        audible=Switch(True),
        record_refresh=0.01,
    )
    try:
        wait_until(record.exists)
        long_ago = time.time() - 120
        os.utime(record, (long_ago, long_ago))
        wait_until(lambda: time.time() - record.stat().st_mtime < 5)
    finally:
        guard.stop()


def test_restore_continues_past_an_unplugged_microphone(tmp_path: Path) -> None:
    record = tmp_path / "microphone-mute.json"
    record.write_text(json.dumps({"devices": ["unplugged", "headset"]}), encoding="utf-8")
    devices = FakeDevices({"headset": True})
    messages: list[str] = []

    restore_microphones(devices, record, messages.append)

    assert devices.muted["headset"] is False
    assert not record.exists()
    assert len(messages) == 1


@pytest.mark.parametrize("contents", ["not json", "[]", '{"devices": 1}'])
def test_restore_discards_an_unreadable_record(tmp_path: Path, contents: str) -> None:
    record = tmp_path / "microphone-mute.json"
    record.write_text(contents, encoding="utf-8")
    devices = FakeDevices({"headset": True})

    restore_microphones(devices, record, lambda _message: None)

    assert devices.muted["headset"] is True
    assert not record.exists()


class FakePlayback:
    def set_paused(self, _paused: bool) -> bool:
        return True


def test_playback_is_audible_only_while_attached_and_not_paused() -> None:
    paused = threading.Event()
    control = LivePlaybackControl(paused.is_set)
    playback = FakePlayback()
    assert not control.audible()

    control.attach(playback)
    assert control.audible()
    paused.set()
    assert not control.audible()
    paused.clear()
    control.start_clearing("clear-1")
    assert not control.audible()
    control.finish_clearing("clear-1", hold_active=False)
    assert control.audible()

    control.detach(playback)
    assert not control.audible()


def test_engine_reports_audible_only_while_its_stream_plays(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_microphone_audibility")
    configure_runtime(engine, tmp_path)
    path = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    path.write_text("Speech", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, path)
    audible_while_playing: list[bool] = []

    class Stream:
        active = False

        def __init__(self, *, callback, finished_callback, **_settings):
            self.callback = callback
            self.finished_callback = finished_callback

        def start(self):
            audible_while_playing.append(engine.playback_control.audible())
            try:
                self.callback(np.empty((8, 1), dtype=np.float32), 8, None, None)
            except CallbackStop:
                self.finished_callback()

        def close(self):
            pass

    sounddevice = SimpleNamespace(CallbackStop=CallbackStop, OutputStream=Stream)
    audio = np.full(4, 0.25, dtype=np.float32)
    assert engine.play_one(sounddevice, path, audio, 1000, "piece", queue.Queue(), state) == "done"

    assert audible_while_playing == [True]
    assert not engine.playback_control.audible()
