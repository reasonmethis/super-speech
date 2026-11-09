from __future__ import annotations

import os
import sys
import time

import pytest

import other_audio
from other_audio import OtherAudioMonitor


class FakeOtherAudio:
    def __init__(self) -> None:
        self.sound = False
        self.error: OSError | None = None
        self.reads = 0

    def playing(self) -> bool:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.sound


def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.005)


@pytest.fixture
def short_quiet(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(other_audio, "QUIET_S", 0.15)
    return 0.15


def test_speech_waits_until_other_sound_has_stopped_for_the_quiet_period(
    short_quiet: float,
) -> None:
    sound = FakeOtherAudio()
    monitor = OtherAudioMonitor(
        sound, active=lambda: True, log=lambda _message: None, poll_interval=0.005
    )
    monitor.start()
    try:
        wait_until(lambda: not monitor.should_wait())
        sound.sound = True
        wait_until(monitor.should_wait)
        sound.sound = False
        stopped = time.monotonic()
        wait_until(lambda: not monitor.should_wait())
        assert time.monotonic() - stopped >= short_quiet * 0.9
    finally:
        monitor.stop()


def test_quiet_counts_only_from_when_sampling_began(short_quiet: float) -> None:
    sound = FakeOtherAudio()
    active = False
    monitor = OtherAudioMonitor(
        sound, active=lambda: active, log=lambda _message: None, poll_interval=0.005
    )
    monitor.start()
    try:
        time.sleep(0.05)
        assert sound.reads == 0
        assert not monitor.should_wait()
        active = True
        started = time.monotonic()
        assert monitor.should_wait()
        wait_until(lambda: not monitor.should_wait())
        assert time.monotonic() - started >= short_quiet * 0.9
        # Going inactive releases a waiting reply at once and stops reading
        sound.sound = True
        wait_until(monitor.should_wait)
        active = False
        assert not monitor.should_wait()
        reads = sound.reads
        time.sleep(0.05)
        assert sound.reads == reads
    finally:
        monitor.stop()


def test_a_failing_meter_counts_as_quiet_and_is_logged_once(short_quiet: float) -> None:
    sound = FakeOtherAudio()
    sound.error = OSError("device vanished")
    messages: list[str] = []
    monitor = OtherAudioMonitor(
        sound, active=lambda: True, log=messages.append, poll_interval=0.005
    )
    monitor.start()
    try:
        wait_until(lambda: not monitor.should_wait())
        assert sound.reads >= 1
        assert messages == ["other audio check failed: device vanished"]
    finally:
        monitor.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="reads Windows audio meters")
def test_windows_meters_can_be_read_without_counting_this_process() -> None:
    meter = other_audio.system_other_audio(os.getpid())
    assert meter is not None
    assert isinstance(meter.playing(), bool)
