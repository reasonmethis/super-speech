from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import Future
from pathlib import Path

import pytest

from engine_test_support import configure_runtime, load_engine

from engine_control import (
    EngineControlServer,
    LivePlaybackControl,
    PlaybackStateTracker,
)


def post_control(endpoint: dict[str, object], payload: object, token: str) -> object:
    request = urllib.request.Request(
        f"http://127.0.0.1:{endpoint['port']}/v1/control",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=1) as response:
        return json.load(response)


def test_control_server_authenticates_and_dispatches_requests(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []
    server = EngineControlServer(
        tmp_path,
        123,
        lambda payload: requests.append(payload) or {"state": "paused"},
    )
    server.start()
    try:
        endpoint = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            post_control(endpoint, {"command": "pause"}, "wrong-token")
        assert unauthorized.value.code == 401

        response = post_control(
            endpoint,
            {"command": "pause"},
            endpoint["token"],
        )

        assert response == {"result": {"state": "paused"}}
        assert requests == [{"command": "pause"}]
    finally:
        server.stop()

    assert not (tmp_path / "control.json").exists()


def test_control_server_rejects_invalid_payloads(tmp_path: Path) -> None:
    server = EngineControlServer(tmp_path, 123, lambda payload: payload)
    server.start()
    try:
        endpoint = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
        with pytest.raises(urllib.error.HTTPError) as invalid:
            post_control(endpoint, ["pause"], endpoint["token"])
        assert invalid.value.code == 400
    finally:
        server.stop()


def test_engine_control_stops_live_audio_before_persisting_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_control_commands")
    configure_runtime(engine, tmp_path)
    calls: list[object] = []
    playback_states: list[bool] = []

    class FakePlayback:
        def set_paused(self, paused: bool) -> bool:
            playback_states.append(paused)
            return True

    playback = FakePlayback()
    engine.playback_control.attach(playback)
    paused = {"state": "paused"}
    playing = {"state": "playing"}
    mutation_result = {"outcome": "committed"}

    def observe_publish(signal: Path) -> None:
        calls.append(("publish", signal.name, playback_states[-1]))

    def observe_resume() -> None:
        calls.append(("resume", playback_states[-1]))

    def observe_mutation(request: object) -> object:
        calls.append(("mutate", getattr(request, "type"), playback_states[-1]))
        return mutation_result

    monkeypatch.setattr(
        engine,
        "publish_ordered_marker",
        observe_publish,
    )
    monkeypatch.setattr(engine, "resume", observe_resume)
    monkeypatch.setattr(
        engine,
        "playback_control_ack",
        lambda is_paused: paused if is_paused else playing,
    )
    monkeypatch.setattr(
        engine,
        "execute_mutation_request",
        observe_mutation,
    )

    assert engine.execute_control_request({"command": "pause"}) == paused
    assert engine.execute_control_request({"command": "resume"}) == playing
    assert engine.execute_control_request(
        {"command": "mutate", "mutation": {"type": "clear"}}
    ) == mutation_result
    assert calls == [
        ("publish", "PAUSE", True),
        ("resume", False),
        ("mutate", "clear", True),
    ]
    engine.playback_control.detach(playback)

    with pytest.raises(ValueError, match="invalid engine control request"):
        engine.execute_control_request({"command": "pause", "extra": True})


def test_playback_state_tracker_waits_for_the_audio_loop() -> None:
    tracker = PlaybackStateTracker()

    assert tracker.wait_for({"idle"}, 0.01) == "idle"
    with pytest.raises(RuntimeError, match="did not acknowledge"):
        tracker.wait_for({"paused"}, 0.01)

    tracker.set("playing")
    assert tracker.wait_for({"playing"}, 0.01) == "playing"
    tracker.set("paused")
    assert tracker.wait_for({"paused"}, 0.01) == "paused"


def test_clear_owns_live_audio_until_the_old_stream_detaches() -> None:
    control = LivePlaybackControl(lambda: False)

    class FakePlayback:
        paused = False

        def set_paused(self, paused: bool) -> bool:
            self.paused = paused
            return True

    playback = FakePlayback()
    control.attach(playback)
    control.start_clearing("clear-1")

    assert playback.paused
    assert control.pause_requested()
    control.start_clearing("clear-1")
    with pytest.raises(RuntimeError, match="another Clear"):
        control.start_clearing("clear-2")
    with pytest.raises(RuntimeError, match="while Clear is finishing"):
        control.begin_command(False)

    control.finish_clearing("clear-1", hold_active=True)
    assert playback.paused
    assert control.pause_requested()

    control.detach(playback)
    replacement = FakePlayback()
    assert not control.attach(replacement)
    assert not replacement.paused
    control.detach(replacement)


def test_live_audio_does_not_wait_for_marker_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_async_playback_control")
    configure_runtime(engine, tmp_path)
    submitted: list[tuple[object, tuple[object, ...], Future[None]]] = []
    persisted: list[str] = []

    class FakePlayback:
        paused = False

        def set_paused(self, paused: bool) -> bool:
            self.paused = paused
            return True

    class DeferredExecutor:
        def submit(self, function, *arguments):
            future: Future[None] = Future()
            submitted.append((function, arguments, future))
            return future

    playback = FakePlayback()
    engine.playback_control.attach(playback)
    monkeypatch.setattr(engine, "_playback_marker_executor", DeferredExecutor())
    monkeypatch.setattr(
        engine,
        "playback_control_ack",
        lambda paused: {"state": "paused" if paused else "playing"},
    )
    monkeypatch.setattr(
        engine,
        "publish_ordered_marker",
        lambda signal: persisted.append(signal.name),
    )

    result = engine.execute_control_request({"command": "pause"})

    assert result == {"state": "paused"}
    assert playback.paused
    assert persisted == []

    function, arguments, future = submitted.pop()
    function(*arguments)
    future.set_result(None)
    assert persisted == ["PAUSE"]
    engine.playback_control.detach(playback)
