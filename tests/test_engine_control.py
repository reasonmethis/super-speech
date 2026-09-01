from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import Future
from pathlib import Path

import pytest

from engine_control import EngineControlServer, LivePlaybackControl
from engine_test_support import configure_runtime, load_engine


class FakePlayback:
    paused = False

    def set_paused(self, paused: bool) -> bool:
        self.paused = paused
        return True


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

    endpoint_path = tmp_path / "control.json"
    assert not endpoint_path.exists()

    server.start()
    replacement = json.loads(endpoint_path.read_text(encoding="utf-8"))
    replacement["token"] = "f" * 64
    try:
        endpoint_path.write_text(json.dumps(replacement), encoding="utf-8")
    finally:
        server.stop()

    assert json.loads(endpoint_path.read_text(encoding="utf-8")) == replacement


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

    class RecordingPlayback:
        def set_paused(self, paused: bool) -> bool:
            playback_states.append(paused)
            return True

    playback = RecordingPlayback()
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
        lambda is_paused, _audio_state: paused if is_paused else playing,
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


def test_live_playback_control_reports_the_synchronously_applied_state() -> None:
    persisted_pause = False
    control = LivePlaybackControl(lambda: persisted_pause)

    first_token, audio_state = control.begin_command(True)
    assert audio_state == "idle"

    playback = FakePlayback()
    assert control.attach(playback)
    second_token, audio_state = control.begin_command(False)
    assert audio_state == "playing"
    assert not playback.paused

    control.end_command(first_token)
    assert not playback.paused
    persisted_pause = True
    control.end_command(second_token)
    assert playback.paused
    control.detach(playback)


def test_clear_owns_live_audio_until_the_old_stream_detaches() -> None:
    control = LivePlaybackControl(lambda: False)

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
        lambda paused, _audio_state: {"state": "paused" if paused else "playing"},
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

    def fail_to_publish(_signal: Path) -> None:
        raise OSError("marker unavailable")

    monkeypatch.setattr(engine, "publish_ordered_marker", fail_to_publish)
    engine.execute_control_request({"command": "pause"})
    function, arguments, _future = submitted.pop()

    with pytest.raises(OSError, match="marker unavailable"):
        function(*arguments)

    assert not playback.paused
    engine.playback_control.detach(playback)
