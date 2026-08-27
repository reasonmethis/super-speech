from __future__ import annotations

import importlib.util
import json
import os
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from pauseable_audio import PauseableAudio


class CallbackStop(Exception):
    pass


def load_engine(module_name: str):
    module_path = ENGINE_SOURCE / "super_speech_engine.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine


def configure_runtime(engine, tmp_path: Path) -> None:
    engine.BASE = tmp_path
    engine.QUEUE = tmp_path / "queue"
    engine.SPOKEN = tmp_path / "spoken"
    engine.FAILED = tmp_path / "failed"
    engine.LOG = tmp_path / "log.txt"
    engine.PAUSE = tmp_path / "PAUSE"
    engine.STOP = tmp_path / "STOP"
    engine.INTERRUPT = tmp_path / "INTERRUPT"
    engine.SKIP = tmp_path / "SKIP"
    engine.CLEAR = tmp_path / "CLEAR"
    engine.CONTINUE = tmp_path / "CONTINUE"
    engine.PLAY = tmp_path / "PLAY.json"
    engine.QUEUE_COMMAND = tmp_path / "QUEUE_COMMAND.json"
    engine.QUEUE_ORDER = tmp_path / "queue-order.json"
    engine.HISTORY_ORDER = tmp_path / "history-order.json"
    engine.WARMUP = tmp_path / "WARMUP"
    engine.HEARTBEAT = tmp_path / "engine.alive"
    engine.STATUS = tmp_path / "status.json"
    engine.INSTANCE_LOCK = tmp_path / "engine.lock"
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()


def test_pause_keeps_the_next_sample_position() -> None:
    audio = np.arange(8, dtype=np.float32)
    playback = PauseableAudio(audio, CallbackStop)

    first_output = np.empty((3, 1), dtype=np.float32)
    playback.callback(first_output, 3, None, None)
    np.testing.assert_array_equal(first_output[:, 0], [0, 1, 2])
    assert playback.position == 3

    assert playback.set_paused(True)
    paused_output = np.empty((3, 1), dtype=np.float32)
    playback.callback(paused_output, 3, None, None)
    np.testing.assert_array_equal(paused_output[:, 0], [0, 0, 0])
    assert playback.position == 3

    assert playback.set_paused(False)
    resumed_output = np.empty((3, 1), dtype=np.float32)
    playback.callback(resumed_output, 3, None, None)
    np.testing.assert_array_equal(resumed_output[:, 0], [3, 4, 5])
    assert playback.position == 6


def test_callback_stops_after_the_last_sample() -> None:
    playback = PauseableAudio(np.array([0.25, 0.5], dtype=np.float32), CallbackStop)
    output = np.empty((4, 1), dtype=np.float32)

    with pytest.raises(CallbackStop):
        playback.callback(output, 4, None, None)

    np.testing.assert_array_equal(output[:, 0], [0.25, 0.5, 0, 0])
    assert playback.position == 2
    assert not playback.done.is_set()
    playback.mark_done()
    assert playback.done.is_set()


def test_status_exposes_pause_current_chunk_and_queue(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_status")

    configure_runtime(engine, tmp_path)
    engine.PAUSE.touch()
    (engine.QUEUE / "001-af_heart-say.txt").write_text("Current words", encoding="utf-8")
    full_queue_text = "Queued words " * 40
    for number in range(2, 7):
        (engine.QUEUE / f"{number:03}-bm_fable-say.txt").write_text(
            full_queue_text if number == 2 else f"Queued words {number}",
            encoding="utf-8",
        )

    state = engine.State()
    state.playing = "001-af_heart-say.txt"
    state.current_text = "Current words"
    state.current_voice = "af_heart"
    state.current_piece = 1
    state.current_piece_count = 2
    state.recent_starts = [("001-af_heart-say", 12.5)]

    engine.publish_status("playing", state, force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "paused"
    assert status["current"]["text"] == "Current words"
    assert status["current"]["voice"] == "af_heart"
    assert status["recent_starts"] == [
        {"id": "001-af_heart-say", "started_at": 12.5}
    ]
    assert status["queue_count"] == 5
    assert len(status["queue"]) == 5
    assert status["queue"][0]["text"] == full_queue_text.strip()
    assert status["queue"][-1]["text"] == "Queued words 6"
    assert status["version"] == engine.STATUS_VERSION
    assert status["history_count"] == 0
    assert status["history"] == []


def test_status_stays_playing_while_the_current_item_waits_for_its_next_piece(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_status_between_pieces")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Two sentences. Still one speech item.", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    state.current_text = current.read_text(encoding="utf-8")
    state.current_voice = "af_heart"
    state.current_piece = 1
    state.current_piece_count = 2

    engine.publish_status("idle", state, force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "playing"
    assert status["current"]["id"] == current.stem


def test_started_receipts_are_bounded_and_keep_multiple_fast_items(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_started_receipts")
    configure_runtime(engine, tmp_path)
    state = engine.State()

    for number in range(25):
        engine.record_started(state, f"{number:03d}-af_heart-say", float(number))

    assert len(state.recent_starts) == 20
    assert state.recent_starts[0] == ("024-af_heart-say", 24.0)
    assert state.recent_starts[-1] == ("005-af_heart-say", 5.0)


def test_audio_stream_failure_does_not_publish_a_started_receipt(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_stream_start_failure")
    configure_runtime(engine, tmp_path)
    path = engine.QUEUE / "001-af_heart-say.txt"
    path.write_text("Speech", encoding="utf-8")
    state = engine.State()
    state.playing = path.name

    def fail_output_stream(**_kwargs):
        raise RuntimeError("no audio device")

    sounddevice = SimpleNamespace(
        CallbackStop=RuntimeError,
        OutputStream=fail_output_stream,
    )

    with pytest.raises(RuntimeError, match="no audio device"):
        engine.play_one(
            sounddevice,
            np,
            path,
            np.ones(4, dtype=np.float32),
            1000,
            "chunk",
            queue.Queue(),
            state,
        )

    assert state.recent_starts == []


def test_fatal_engine_stop_cannot_start_another_audio_stream(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_fatal_before_stream")
    configure_runtime(engine, tmp_path)
    path = engine.QUEUE / "001-af_heart-say.txt"
    path.write_text("Do not start", encoding="utf-8")
    state = engine.State()
    state.stop.set()

    def unexpected_stream(**_kwargs):
        raise AssertionError("audio stream must not start")

    sounddevice = SimpleNamespace(
        CallbackStop=RuntimeError,
        OutputStream=unexpected_stream,
    )

    assert engine.play_one(
        sounddevice,
        np,
        path,
        np.ones(4, dtype=np.float32),
        1000,
        "chunk",
        queue.Queue(),
        state,
    ) == "fatal"
    assert state.recent_starts == []


def test_engine_stream_failure_leaves_current_item_visible_in_stopped_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_stream_failure_status")
    configure_runtime(engine, tmp_path)
    engine.MODEL_DIR = tmp_path / "models"
    engine.MODEL_PATH = engine.MODEL_DIR / "model.onnx"
    engine.VOICES_PATH = engine.MODEL_DIR / "voices.bin"
    engine.MODEL_DIR.mkdir()
    engine.MODEL_PATH.touch()
    engine.VOICES_PATH.touch()
    queued = engine.QUEUE / "001-af_heart-say.txt"
    queued.write_text("Keep visible", encoding="utf-8")

    class FakeKokoro:
        @classmethod
        def from_session(cls, *_args, **_kwargs):
            return cls()

        def get_voices(self) -> list[str]:
            return ["af_heart"]

    class FakeSessionOptions:
        intra_op_num_threads = 0

    def deliver_one(_kokoro, buf: queue.Queue, state) -> None:
        with state.lock:
            state.claimed.add(queued.name)
        buf.put(
            (
                queued,
                np.ones(4, dtype=np.float32),
                1000,
                True,
                True,
                1,
                1,
                "Keep visible",
                "af_heart",
            )
        )
        state.stop.wait()

    fake_sounddevice = SimpleNamespace(
        play=lambda *_args, **_kwargs: None,
        wait=lambda: None,
    )
    fake_onnxruntime = SimpleNamespace(
        SessionOptions=FakeSessionOptions,
        InferenceSession=lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=FakeKokoro))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_onnxruntime)
    monkeypatch.setattr(engine, "warmup", lambda _kokoro: None)
    monkeypatch.setattr(engine, "synth_worker", deliver_one)

    def fail_playback(*_args, **_kwargs):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(engine, "play_one", fail_playback)

    with pytest.raises(RuntimeError, match="no audio device"):
        engine.run_engine_loop()

    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"
    assert status["current"]["id"] == queued.stem
    assert status["queue"] == []


def test_status_stays_playing_while_queued_audio_is_being_prepared(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_status_preparing")
    configure_runtime(engine, tmp_path)
    queued = engine.QUEUE / "001-af_heart-say.txt"
    queued.write_text("Still being prepared", encoding="utf-8")

    engine.publish_status("idle", engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "playing"
    assert status["current"]["id"] == queued.stem
    assert status["current"]["piece"] == 0
    assert status["queue"] == []

    queued.unlink()
    engine.publish_status("idle", engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "idle"
    assert status["queue"] == []


def test_status_cannot_be_paused_without_current_or_waiting_speech(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_status_empty_pause")
    configure_runtime(engine, tmp_path)
    engine.PAUSE.touch()

    engine.publish_status("paused", engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "idle"
    assert status["current"] is None
    assert status["queue"] == []


def test_status_count_matches_the_items_in_the_same_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status_read_failure")
    configure_runtime(engine, tmp_path)
    readable = engine.QUEUE / "001-af_heart-say.txt"
    unreadable = engine.QUEUE / "002-af_heart-say.txt"
    readable.write_text("Readable", encoding="utf-8")
    unreadable.write_text("Temporarily locked", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if path == unreadable:
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    engine.publish_status("idle", engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["current"]["id"] == readable.stem
    assert status["queue_count"] == len(status["queue"]) == 0


def test_status_command_retries_a_transient_windows_read_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = load_engine("super_speech_engine_status_retry")
    configure_runtime(engine, tmp_path)
    engine.STATUS.write_text(
        json.dumps({"version": engine.STATUS_VERSION, "state": "idle"}),
        encoding="utf-8",
    )
    original_read_text = Path.read_text
    attempts = 0

    def read_text(path: Path, *args, **kwargs) -> str:
        nonlocal attempts
        if path == engine.STATUS:
            attempts += 1
            if attempts == 1:
                raise PermissionError("status is being replaced")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    engine.print_status()

    assert attempts == 2
    assert json.loads(capsys.readouterr().out)["state"] == "idle"


def test_stopped_status_contains_the_same_queue_items_as_its_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = load_engine("super_speech_engine_stopped_status")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")

    engine.print_status()
    status = json.loads(capsys.readouterr().out)

    assert status["current"]["id"] == waiting.stem
    assert status["queue_count"] == len(status["queue"]) == 0


def test_enqueue_text_reserves_the_next_queue_number(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_enqueue")

    configure_runtime(engine, tmp_path)
    (engine.SPOKEN / "007-af_heart-say.txt").write_text("Earlier", encoding="utf-8")

    queued = engine.enqueue_text("New words", "bm_fable", 650)

    assert queued.name == "008-bm_fable-g650-say.txt"
    assert queued.read_text(encoding="utf-8") == "New words"


def test_enqueue_publishes_the_final_queue_path_only_after_writing_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_enqueue_atomic")
    configure_runtime(engine, tmp_path)
    observed = []
    real_replace = engine.os.replace

    def replace(source, destination) -> None:
        destination = Path(destination)
        if destination.suffix == ".txt":
            observed.append((destination.exists(), Path(source).read_text(encoding="utf-8")))
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", replace)

    queued = engine.enqueue_text("Complete text", "af_heart")

    assert observed == [(False, "Complete text")]
    assert queued.read_text(encoding="utf-8") == "Complete text"


def test_queue_order_preserves_stable_ids_and_appends_new_arrivals(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_queue_order")
    configure_runtime(engine, tmp_path)
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-bm_fable-say.txt"
    third = engine.QUEUE / "003-af_bella-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")

    engine.save_queue_order([third, first, second])
    new_arrival = engine.QUEUE / "004-bm_george-say.txt"
    new_arrival.write_text("New", encoding="utf-8")

    assert [path.stem for path in engine.queue_files_in_order()] == [
        third.stem,
        first.stem,
        second.stem,
        new_arrival.stem,
    ]


def test_moving_a_waiting_chunk_resets_banked_audio_but_keeps_current(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_queue_move")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-bm_fable-say.txt"
    third = engine.QUEUE / "003-af_bella-say.txt"
    for path in (current, second, third):
        path.write_text(path.stem, encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put((current, "current piece"))
    buffered.put((second, "stale second"))
    buffered.put((third, "stale third"))
    state = engine.State()
    state.playing = current.name
    state.claimed.update(path.name for path in (current, second, third))

    engine.apply_queue_command(buffered, state, "move", third.stem, second.stem)

    assert [path.stem for path in engine.queue_files_in_order()] == [
        current.stem,
        third.stem,
        second.stem,
    ]
    assert buffered.get_nowait() == (current, "current piece")
    assert buffered.empty()
    assert state.claimed == {current.name}
    assert state.selection_name is None
    assert engine.claim_next_queued_chunk(state) == third


def test_archiving_one_waiting_chunk_preserves_current_and_remaining_queue(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_queue_archive")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    archived = engine.QUEUE / "002-bm_fable-say.txt"
    remaining = engine.QUEUE / "003-af_bella-say.txt"
    for path in (current, archived, remaining):
        path.write_text(path.stem, encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put((current, "current piece"))
    buffered.put((archived, "stale archived"))
    buffered.put((remaining, "stale remaining"))
    state = engine.State()
    state.playing = current.name
    state.claimed.update(path.name for path in (current, archived, remaining))

    engine.apply_queue_command(buffered, state, "archive", archived.stem, None)

    assert not archived.exists()
    assert (engine.SPOKEN / archived.name).is_file()
    assert [path.stem for path in engine.queue_files_in_order()] == [
        current.stem,
        remaining.stem,
    ]
    assert buffered.get_nowait() == (current, "current piece")
    assert state.claimed == {current.name}


def test_queue_mutation_cannot_cancel_an_accepted_selection(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_selected_queue_mutation")
    configure_runtime(engine, tmp_path)
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()
    state.selection_name = selected.name

    with pytest.raises(ValueError, match="selected speech is starting"):
        engine.apply_queue_command(
            queue.Queue(), state, "archive", selected.stem, None
        )

    assert selected.exists()
    assert state.selection_name == selected.name


def test_deleting_one_history_chunk_preserves_waiting_queue(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_delete")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "002-af_heart-say.txt"
    history = engine.SPOKEN / "001-bm_fable-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    history.write_text("History", encoding="utf-8")
    assert engine.history_snapshot()[0] == 1

    engine.apply_queue_command(queue.Queue(), engine.State(), "delete", history.stem, None)

    assert waiting.read_text(encoding="utf-8") == "Waiting"
    assert not history.exists()
    assert engine.history_snapshot() == (0, [])


def test_deleting_an_already_absent_history_chunk_is_idempotent(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_delete_absent")
    configure_runtime(engine, tmp_path)

    engine.apply_queue_command(
        queue.Queue(),
        engine.State(),
        "delete",
        "001-af_heart-say",
        None,
    )

    assert engine.history_snapshot() == (0, [])


def test_history_delete_is_rejected_while_the_same_item_is_active(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_history_delete_active")
    configure_runtime(engine, tmp_path)
    queued = engine.QUEUE / "001-bm_fable-say.txt"
    history = engine.SPOKEN / queued.name
    queued.write_text("Active replay", encoding="utf-8")
    history.write_text("Active replay", encoding="utf-8")

    with pytest.raises(ValueError, match="history chunk is active"):
        engine.apply_queue_command(
            queue.Queue(), engine.State(), "delete", history.stem, None
        )

    assert queued.exists()
    assert history.exists()


def test_history_delete_succeeds_when_the_order_sidecar_cannot_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_delete_order_failure")
    configure_runtime(engine, tmp_path)
    history = engine.SPOKEN / "001-bm_fable-say.txt"
    history.write_text("History", encoding="utf-8")
    assert engine.history_snapshot()[0] == 1

    def fail_order_update(*_args) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(engine, "save_history_order", fail_order_update)

    engine.apply_queue_command(queue.Queue(), engine.State(), "delete", history.stem, None)

    assert not history.exists()
    assert engine.history_snapshot() == (0, [])


def test_archive_still_succeeds_when_the_order_sidecar_cannot_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_order_failure")
    configure_runtime(engine, tmp_path)
    queued = engine.QUEUE / "001-af_heart-say.txt"
    queued.write_text("Speech", encoding="utf-8")

    def fail_order_update(*_args) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(engine, "save_queue_order", fail_order_update)

    assert engine.archive(queued)
    assert (engine.SPOKEN / queued.name).read_text(encoding="utf-8") == "Speech"


def test_queue_request_is_applied_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_request")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-bm_fable-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")

    request_id = engine.request_queue_command("move", second.stem, first.stem)
    assert engine.process_queue_requests(queue.Queue(), engine.State())
    engine.wait_for_queue_ack(request_id, timeout=0.1)

    assert [path.stem for path in engine.queue_files_in_order()] == [
        second.stem,
        first.stem,
    ]


def test_timed_out_unclaimed_queue_request_cannot_apply_later(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_timeout")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Keep me", encoding="utf-8")
    request_id = engine.request_queue_command("archive", waiting.stem)

    with pytest.raises(RuntimeError, match="did not acknowledge"):
        engine.wait_for_queue_ack(request_id, timeout=0.01)

    assert not engine.process_queue_requests(queue.Queue(), engine.State())
    assert waiting.exists()


def test_timeout_retries_a_transient_request_cancellation_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_cancel_retry")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = engine.request_queue_command("delete", "001-af_heart-say")
    real_replace = engine.os.replace
    failures = 0

    def replace(source, destination) -> None:
        nonlocal failures
        if Path(source).name.endswith(f"{request_id}.json") and failures < 2:
            failures += 1
            raise PermissionError("temporarily locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", replace)

    with pytest.raises(RuntimeError, match="did not acknowledge"):
        engine.wait_for_queue_ack(request_id, timeout=0.01)

    assert failures == 2
    assert not list(engine.BASE.glob(f"QUEUE_COMMAND.*.{request_id}.json"))


def test_persistently_locked_unclaimed_request_returns_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_cancel_locked")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = engine.request_queue_command("delete", "001-af_heart-say")
    monkeypatch.setattr(engine, "cancel_unclaimed_request", lambda *_args: False)

    with pytest.raises(RuntimeError, match="result was unconfirmed"):
        engine.wait_for_queue_ack(request_id, timeout=0.01)

    assert list(engine.BASE.glob(f"QUEUE_COMMAND.*.{request_id}.json"))


def test_ack_published_at_the_timeout_boundary_is_still_observed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_ack_boundary")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = engine.request_queue_command("delete", "001-af_heart-say")
    request = next(engine.BASE.glob(f"QUEUE_COMMAND.*.{request_id}.json"))
    real_wait = engine.wait_for_ack_payload
    calls = 0

    def wait(target: Path, deadline: float):
        nonlocal calls
        calls += 1
        if calls == 1:
            request.unlink()
            assert engine.publish_queue_ack(request_id)
            return None
        return real_wait(target, deadline)

    monkeypatch.setattr(engine, "wait_for_ack_payload", wait)

    engine.wait_for_queue_ack(request_id, timeout=0.01)

    assert calls == 2


def test_history_delete_request_is_applied_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_delete_request")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    history = engine.SPOKEN / "001-af_heart-say.txt"
    history.write_text("History", encoding="utf-8")

    request_id = engine.request_queue_command("delete", history.stem)
    assert engine.process_queue_requests(queue.Queue(), engine.State())
    engine.wait_for_queue_ack(request_id, timeout=0.1)

    assert not history.exists()


def test_queue_ack_retries_a_transient_windows_read_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_ack_retry")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    acknowledgement = engine.queue_ack_path(request_id)
    acknowledgement.write_text(
        json.dumps({"ok": True, "accepted_at": 1.0, "error": None}),
        encoding="utf-8",
    )
    original_read_text = Path.read_text
    attempts = 0

    def read_text(path: Path, *args, **kwargs) -> str:
        nonlocal attempts
        if path == acknowledgement:
            attempts += 1
            if attempts == 1:
                raise PermissionError("acknowledgement is being replaced")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    engine.wait_for_queue_ack(request_id, timeout=0.2)

    assert attempts == 2
    assert not acknowledgement.exists()


def test_queue_request_rejects_the_current_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_current")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    state.playing = current.name

    request_id = engine.request_queue_command("archive", current.stem)
    assert not engine.process_queue_requests(queue.Queue(), state)

    with pytest.raises(RuntimeError, match="waiting chunk not found"):
        engine.wait_for_queue_ack(request_id, timeout=0.1)
    assert current.is_file()


def test_enqueue_text_skips_a_number_reserved_by_another_writer(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_enqueue_race")
    configure_runtime(engine, tmp_path)
    (engine.QUEUE / "001.reserve").touch()

    queued = engine.enqueue_text("New words", "af_heart")

    assert queued.name == "002-af_heart-say.txt"


@pytest.mark.parametrize(
    ("voice", "gap_ms"),
    [("heart", None), ("af_heart", -1), ("af_heart", 1501)],
)
def test_enqueue_text_rejects_invalid_metadata(
    tmp_path: Path, voice: str, gap_ms: int | None
) -> None:
    engine = load_engine("super_speech_engine_invalid")
    configure_runtime(engine, tmp_path)

    with pytest.raises(ValueError):
        engine.enqueue_text("New words", voice, gap_ms)


def test_speak_command_starts_engine_before_queueing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = load_engine("super_speech_engine_cli")
    configure_runtime(engine, tmp_path)
    queued = tmp_path / "001-af_heart-g300-say.txt"
    calls: list[object] = []

    monkeypatch.setattr(engine, "start_engine", lambda: calls.append("start"))

    def enqueue(text: str, voice: str, gap_ms: int | None) -> Path:
        calls.append((text, voice, gap_ms))
        return queued

    monkeypatch.setattr(engine, "enqueue_text", enqueue)
    monkeypatch.setattr(
        engine, "wait_for_queue_acceptance", lambda: calls.append("accept")
    )

    assert engine.cli(["speak", "Hello there", "--gap-ms", "300"]) == 0
    assert calls == ["start", ("Hello there", "af_heart", 300), "accept"]
    assert capsys.readouterr().out.strip() == str(queued)


def test_start_engine_waits_for_fresh_status_after_startup_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_start_ready")
    configure_runtime(engine, tmp_path)
    engine.MODEL_PATH = tmp_path / "model.onnx"
    engine.VOICES_PATH = tmp_path / "voices.bin"
    engine.MODEL_PATH.touch()
    engine.VOICES_PATH.touch()
    fake_pid = 4321
    engine.STATUS.write_text(
        json.dumps(
            {"version": engine.STATUS_VERSION, "engine_pid": 1111, "updated_at": 0}
        ),
        encoding="utf-8",
    )
    running_checks = 0
    sleeps = 0

    def engine_running() -> bool:
        nonlocal running_checks
        running_checks += 1
        return running_checks > 1

    class FakeProcess:
        pid = fake_pid

        @staticmethod
        def poll() -> None:
            return None

    def publish_ready(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        engine.STATUS.write_text(
            json.dumps(
                {
                    "version": engine.STATUS_VERSION,
                    "engine_pid": fake_pid,
                    "updated_at": engine.time.time() + 1,
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(engine, "engine_is_running", engine_running)
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(engine.time, "sleep", publish_ready)

    engine.start_engine()

    assert sleeps == 1


def test_start_engine_accepts_an_existing_current_engine_that_is_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_existing_start_ready")
    configure_runtime(engine, tmp_path)
    engine.STATUS.write_text(
        json.dumps(
            {"version": engine.STATUS_VERSION, "engine_pid": 4321, "updated_at": 0}
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    monkeypatch.setattr(engine, "process_exists", lambda process_id: process_id == 4321)
    monkeypatch.setattr(
        engine.time,
        "sleep",
        lambda _seconds: pytest.fail("current status is already safe for commands"),
    )
    monkeypatch.setattr(
        engine.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("must not launch a second engine"),
    )

    engine.start_engine()


def test_process_exists_recognizes_the_current_process() -> None:
    engine = load_engine("super_speech_engine_process_exists")

    assert engine.process_exists(os.getpid())


def test_start_engine_ignores_status_from_a_previous_lock_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_existing_stale_owner")
    configure_runtime(engine, tmp_path)
    engine.STATUS.write_text(
        json.dumps(
            {"version": engine.STATUS_VERSION, "engine_pid": 1111, "updated_at": 1}
        ),
        encoding="utf-8",
    )
    sleeps = 0

    def publish_current_owner(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        engine.STATUS.write_text(
            json.dumps(
                {"version": engine.STATUS_VERSION, "engine_pid": 2222, "updated_at": 2}
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    monkeypatch.setattr(engine, "process_exists", lambda process_id: process_id == 2222)
    monkeypatch.setattr(engine.time, "sleep", publish_current_owner)

    engine.start_engine()

    assert sleeps == 1


def test_start_engine_rejects_the_previous_selection_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_existing_v1")
    configure_runtime(engine, tmp_path)
    engine.STATUS.write_text(
        json.dumps(
            {
                "version": engine.STATUS_VERSION - 1,
                "engine_pid": 4321,
                "updated_at": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    with pytest.raises(
        RuntimeError,
        match=rf"unsupported protocol version {engine.STATUS_VERSION - 1}",
    ):
        engine.start_engine()


def test_pause_and_resume_commands_share_the_runtime_signal(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_pause")
    configure_runtime(engine, tmp_path)

    assert engine.cli(["pause"]) == 0
    assert engine.PAUSE.is_file()
    assert engine.cli(["resume"]) == 0
    assert not engine.PAUSE.exists()


def test_scoped_control_cannot_affect_a_successor_engine(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_scoped_control")
    configure_runtime(engine, tmp_path)

    engine.INTERRUPT.write_text(
        json.dumps({"engine_pid": os.getpid() + 1}), encoding="utf-8"
    )
    assert not engine.consume_control(engine.INTERRUPT)
    assert not engine.INTERRUPT.exists()

    engine.INTERRUPT.write_text(
        json.dumps({"engine_pid": os.getpid()}), encoding="utf-8"
    )
    assert engine.consume_control(engine.INTERRUPT)
    assert not engine.INTERRUPT.exists()


def test_play_requests_acknowledge_a_superseded_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_signal")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    first_request_id = engine.request_play("001-af_heart-say")
    second_request_id = engine.request_play("002-bm_fable-say")

    taken = engine.take_play_request()
    assert taken is not None
    assert taken[:3] == ("002-bm_fable-say", None, second_request_id)
    taken[3].unlink()
    assert engine.take_play_request() is None
    with pytest.raises(RuntimeError, match="superseded by a newer play request"):
        engine.wait_for_play_ack(first_request_id, timeout=0.1)


def test_interrupt_rejects_pending_requests_without_a_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_interrupt_requests")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    selected = engine.QUEUE / "001-af_heart-say.txt"
    selected.write_text("Selected", encoding="utf-8")
    play_request = engine.request_play(selected.stem)
    queue_request = engine.request_queue_command("move", selected.stem)
    engine.INTERRUPT.touch()

    assert engine.gap_wait(1.0, queue.Queue(), engine.State()) == "interrupt"
    with pytest.raises(RuntimeError, match="interrupted"):
        engine.wait_for_play_ack(play_request, timeout=0.1)
    with pytest.raises(RuntimeError, match="interrupted"):
        engine.wait_for_queue_ack(queue_request, timeout=0.1)


def test_play_command_starts_engine_then_publishes_the_requested_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_cli")
    configure_runtime(engine, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(engine, "start_engine", lambda: calls.append("start"))
    def request(chunk_id: str, voice: str | None) -> str:
        calls.append((chunk_id, voice))
        return "a" * 24

    monkeypatch.setattr(engine, "request_play", request)
    monkeypatch.setattr(
        engine,
        "wait_for_play_ack",
        lambda request_id: calls.append(("wait", request_id))
        or {"id": "007-bm_fable-say", "accepted_at": 1.0},
    )

    assert engine.cli(["play", "007-bm_fable-say", "--voice", "af_heart"]) == 0
    assert calls == [
        "start",
        ("007-bm_fable-say", "af_heart"),
        ("wait", "a" * 24),
    ]


def test_play_request_rejects_a_non_id_before_touching_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_invalid")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    with pytest.raises(ValueError):
        engine.request_play("../spoken/007")

    assert not engine.PLAY.exists()


def test_playing_current_chunk_resumes_without_reordering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_current")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    engine.PAUSE.touch()
    engine.STOP.touch()
    buffered: queue.Queue = queue.Queue()
    buffered.put("banked piece")
    state = engine.State()
    state.playing = current.name
    state.claimed.add(current.name)
    state.saw_stop = True

    request_id = engine.request_play(current.stem)
    assert engine.process_play_request(buffered, state) is None
    acceptance = engine.wait_for_play_ack(request_id, timeout=0.1)

    assert not engine.PAUSE.exists()
    assert not engine.STOP.exists()
    assert not state.saw_stop
    assert state.claimed == {current.name}
    assert buffered.get_nowait() == "banked piece"
    assert acceptance["id"] == current.stem


def test_selecting_upcoming_chunk_archives_everything_older(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_upcoming")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    older = engine.QUEUE / "002-af_heart-say.txt"
    selected = engine.QUEUE / "003-bm_fable-say.txt"
    newer = engine.QUEUE / "004-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    older.write_text("Older waiting", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    newer.write_text("Newer waiting", encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put("banked piece")
    state = engine.State()
    state.playing = current.name
    state.claimed.update({current.name, older.name, selected.name})

    request_id = engine.request_play(selected.stem)
    assert engine.process_play_request(buffered, state) == "select"
    acceptance = engine.wait_for_play_ack(request_id, timeout=0.1)

    assert not current.exists()
    assert not older.exists()
    assert (engine.SPOKEN / current.name).read_text(encoding="utf-8") == "Current"
    assert (engine.SPOKEN / older.name).read_text(encoding="utf-8") == "Older waiting"
    assert state.claimed == set()
    assert buffered.empty()

    assert engine.finish_chunk_playback(current, "select", False, state)
    assert state.playing is None
    assert engine.claim_next_queued_chunk(state) == selected
    assert engine.claim_next_queued_chunk(state) == newer
    assert engine.claim_next_queued_chunk(state) is None
    assert acceptance["id"] == selected.stem


def test_selecting_waiting_chunk_without_playback_archives_older_waiting_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_waiting")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    older = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    newer = engine.QUEUE / "003-af_heart-say.txt"
    for path in (older, selected, newer):
        path.write_text(path.stem, encoding="utf-8")

    engine.request_play(selected.stem)
    assert engine.process_play_request(queue.Queue(), engine.State()) == "select"

    assert not older.exists()
    assert (engine.SPOKEN / older.name).exists()
    assert engine.queue_files_in_order() == [selected, newer]


def test_selecting_waiting_chunk_rejects_an_archive_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_archive_failure")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    older = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    older.write_text("Older", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    monkeypatch.setattr(engine, "archive", lambda _path: False)
    state = engine.State()

    request_id = engine.request_play(selected.stem)
    assert engine.process_play_request(queue.Queue(), state) is None

    with pytest.raises(RuntimeError, match="could not select"):
        engine.wait_for_play_ack(request_id, timeout=0.1)
    assert state.selection_name is None
    assert older.exists()
    assert selected.exists()


def test_selection_rolls_back_earlier_archives_when_a_later_archive_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_archive_rollback")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    older = [
        engine.QUEUE / "001-af_heart-say.txt",
        engine.QUEUE / "002-af_heart-say.txt",
    ]
    selected = engine.QUEUE / "003-bm_fable-say.txt"
    for path in [*older, selected]:
        path.write_text(path.stem, encoding="utf-8")
    real_archive = engine.archive
    calls = 0

    def fail_second(path: Path) -> bool:
        nonlocal calls
        calls += 1
        return real_archive(path) if calls == 1 else False

    monkeypatch.setattr(engine, "archive", fail_second)
    request_id = engine.request_play(selected.stem)
    assert engine.process_play_request(queue.Queue(), engine.State()) is None

    with pytest.raises(RuntimeError, match="could not select"):
        engine.wait_for_play_ack(request_id, timeout=0.1)
    assert engine.queue_files_in_order() == [*older, selected]
    assert not list(engine.SPOKEN.glob("*.txt"))


def test_voice_selection_rolls_back_when_archiving_an_older_item_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_voice_archive_rollback")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    older = [
        engine.QUEUE / "001-af_heart-say.txt",
        engine.QUEUE / "002-af_heart-say.txt",
    ]
    selected = engine.QUEUE / "003-af_heart-say.txt"
    for path in [*older, selected]:
        path.write_text(path.stem, encoding="utf-8")
    real_archive = engine.archive
    calls = 0

    def fail_second(path: Path) -> bool:
        nonlocal calls
        calls += 1
        return False if calls == 2 else real_archive(path)

    monkeypatch.setattr(engine, "archive", fail_second)
    request_id = engine.request_play(selected.stem, "bm_fable")
    assert engine.process_play_request(queue.Queue(), engine.State()) is None

    with pytest.raises(RuntimeError, match="could not select"):
        engine.wait_for_play_ack(request_id, timeout=0.1)
    assert engine.queue_files_in_order() == [*older, selected]
    assert not list(engine.SPOKEN.glob("*.txt"))


def test_replaying_history_reuses_its_id_without_duplicating_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_history")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    archived = engine.SPOKEN / "007-bm_fable-g350-say.txt"
    archived.write_text("Say this again", encoding="utf-8")
    state = engine.State()

    request_id = engine.request_play(archived.stem)
    assert engine.process_play_request(queue.Queue(), state) == "select"

    replay = engine.QUEUE / archived.name
    assert not archived.exists()
    assert replay.read_text(encoding="utf-8") == "Say this again"
    assert engine.claim_next_queued_chunk(state) == replay
    assert engine.wait_for_play_ack(request_id, timeout=0.1)["id"] == archived.stem
    assert engine.archive(replay)
    assert [path.name for path in engine.SPOKEN.glob("*.txt")] == [archived.name]


@pytest.mark.parametrize("history_index", range(5))
@pytest.mark.parametrize("include_active", [False, True])
def test_history_selection_moves_only_the_playback_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    history_index: int,
    include_active: bool,
) -> None:
    engine = load_engine(
        f"super_speech_engine_history_boundary_{history_index}_{include_active}"
    )
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    history = [
        engine.SPOKEN / f"{number:03d}-af_heart-say.txt"
        for number in range(5, 0, -1)
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order(history)
    if include_active:
        active = [
            engine.QUEUE / "006-af_heart-say.txt",
            engine.QUEUE / "007-af_heart-say.txt",
        ]
        for path in active:
            path.write_text(path.stem, encoding="utf-8")
        engine.save_queue_order(active)

    def visible_ids() -> list[str]:
        status = json.loads(engine.STATUS.read_text(encoding="utf-8"))
        active_ids = {
            *[item["id"] for item in status["queue"]],
            *([status["current"]["id"]] if status["current"] else []),
        }
        return [
            *[item["id"] for item in reversed(status["queue"])],
            *([status["current"]["id"]] if status["current"] else []),
            *[
                item["id"]
                for item in status["history"]
                if item["id"] not in active_ids
            ],
        ]

    state = engine.State()
    engine.publish_status("idle", state, force=True)
    original_order = visible_ids()
    selected = history[history_index]

    request_id = engine.request_play(selected.stem)
    assert engine.process_play_request(queue.Queue(), state) == "select"
    assert engine.wait_for_play_ack(request_id, timeout=0.1)["id"] == selected.stem
    selected_status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert selected_status["current"]["id"] == selected.stem
    assert visible_ids() == original_order

    restarted = engine.State()
    engine.publish_status("idle", restarted, force=True)
    assert visible_ids() == original_order
    assert json.loads(engine.STATUS.read_text(encoding="utf-8"))["current"]["id"] == selected.stem

    selected_in_queue = engine.QUEUE / selected.name
    assert engine.finish_chunk_playback(selected_in_queue, "done", True, restarted)
    engine.publish_status("idle", restarted, force=True)
    assert visible_ids() == original_order


def test_history_replay_stays_first_after_an_engine_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_replay_restart")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    archived = engine.SPOKEN / "001-bm_fable-say.txt"
    first = engine.QUEUE / "010-af_heart-say.txt"
    second = engine.QUEUE / "011-af_heart-say.txt"
    for path in (archived, first, second):
        path.write_text(path.stem, encoding="utf-8")
    engine.save_queue_order([first, second])

    request_id = engine.request_play(archived.stem)
    assert engine.process_play_request(queue.Queue(), engine.State()) == "select"
    engine.wait_for_play_ack(request_id, timeout=0.1)

    assert engine.claim_next_queued_chunk(engine.State()) == engine.QUEUE / archived.name


def test_history_selection_rolls_back_if_a_boundary_item_cannot_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_boundary_rollback")
    configure_runtime(engine, tmp_path)
    history = [
        engine.SPOKEN / "003-af_heart-say.txt",
        engine.SPOKEN / "002-af_heart-say.txt",
        engine.SPOKEN / "001-bm_fable-say.txt",
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order(history)
    real_replace = engine.os.replace
    moves = 0

    def replace(source, destination) -> None:
        nonlocal moves
        if Path(source).parent == engine.SPOKEN and Path(destination).parent == engine.QUEUE:
            moves += 1
            if moves == 2:
                raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", replace)

    with pytest.raises(RuntimeError, match="could not move History playback boundary"):
        engine.promote_history_selection(history[-1])

    assert engine.history_files_in_order() == history
    assert not list(engine.QUEUE.glob("*.txt"))


def test_history_selection_excludes_worker_claims_until_rollback_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_boundary_worker_lock")
    configure_runtime(engine, tmp_path)
    source = engine.SPOKEN / "001-af_heart-say.txt"
    source.write_text("History", encoding="utf-8")
    entered_save = threading.Event()
    release_save = threading.Event()
    real_save = engine.save_queue_order

    def fail_after_worker_waits(paths=None) -> None:
        entered_save.set()
        assert release_save.wait(1)
        raise PermissionError("locked")

    monkeypatch.setattr(engine, "save_queue_order", fail_after_worker_waits)
    promotion_errors = []

    def promote() -> None:
        try:
            engine.promote_history_selection(source)
        except RuntimeError as error:
            promotion_errors.append(error)

    promotion = threading.Thread(target=promote)
    promotion.start()
    assert entered_save.wait(1)
    state = engine.State()
    claimed = []
    worker = threading.Thread(
        target=lambda: claimed.append(engine.claim_next_queued_chunk(state))
    )
    worker.start()
    assert worker.is_alive()
    assert state.claimed == set()
    release_save.set()
    promotion.join(1)
    worker.join(1)
    monkeypatch.setattr(engine, "save_queue_order", real_save)

    assert promotion_errors
    assert claimed == [None]
    assert state.claimed == set()
    assert source.exists()


def test_history_selection_rollback_preserves_a_legacy_queue_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_legacy_duplicate")
    configure_runtime(engine, tmp_path)
    duplicate_history = engine.SPOKEN / "003-af_heart-say.txt"
    duplicate_queue = engine.QUEUE / duplicate_history.name
    selected = engine.SPOKEN / "002-af_heart-say.txt"
    duplicate_history.write_text("Legacy replay", encoding="utf-8")
    duplicate_queue.write_text("Active copy", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    engine.save_history_order([duplicate_history, selected])
    real_replace = engine.os.replace

    def fail_selected(source, destination) -> None:
        if Path(source) == selected:
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_selected)

    with pytest.raises(RuntimeError, match="could not move History playback boundary"):
        engine.promote_history_selection(selected)

    assert duplicate_queue.read_text(encoding="utf-8") == "Active copy"
    assert duplicate_history.read_text(encoding="utf-8") == "Legacy replay"
    assert selected.read_text(encoding="utf-8") == "Selected"


def test_history_selection_reports_unconfirmed_if_rollback_cannot_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_unconfirmed")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    first = engine.SPOKEN / "002-af_heart-say.txt"
    selected = engine.SPOKEN / "001-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    engine.save_history_order([first, selected])
    real_replace = engine.os.replace

    def fail_forward_and_rollback(source, destination) -> None:
        source = Path(source)
        destination = Path(destination)
        if source == selected or (
            source == engine.QUEUE / first.name and destination == first
        ):
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_forward_and_rollback)
    request_id = engine.request_play(selected.stem)

    assert engine.process_play_request(queue.Queue(), engine.State()) is None
    with pytest.raises(RuntimeError, match="result was unconfirmed"):
        engine.wait_for_play_ack(request_id, timeout=0.1)


def test_replaying_history_with_another_voice_preserves_text_and_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_history_voice")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    archived = engine.SPOKEN / "007-bm_fable-g350-say.txt"
    archived.write_text("Say this another way", encoding="utf-8")
    state = engine.State()

    request_id = engine.request_play(archived.stem, "af_heart")
    assert engine.process_play_request(queue.Queue(), state) == "select"
    acceptance = engine.wait_for_play_ack(request_id, timeout=0.1)
    variant = engine.QUEUE / f"{acceptance['id']}.txt"

    assert not archived.exists()
    assert variant.name == "007-af_heart-g350-say.txt"
    assert variant.read_text(encoding="utf-8") == "Say this another way"
    assert engine.claim_next_queued_chunk(state) == variant


def test_history_voice_change_keeps_the_row_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_voice_position")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    history = [
        engine.SPOKEN / "003-af_heart-say.txt",
        engine.SPOKEN / "002-af_heart-say.txt",
        engine.SPOKEN / "001-af_heart-say.txt",
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order(history)
    state = engine.State()
    engine.publish_status("idle", state, force=True)
    before = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    request_id = engine.request_play(history[1].stem, "bm_fable")
    assert engine.process_play_request(queue.Queue(), state) == "select"
    acceptance = engine.wait_for_play_ack(request_id, timeout=0.1)
    after = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert [item["text"] for item in before["history"]] == [
        *[item["text"] for item in reversed(after["queue"])],
        after["current"]["text"],
        *[item["text"] for item in after["history"]],
    ]
    assert after["current"]["id"] == acceptance["id"]
    assert after["current"]["voice"] == "bm_fable"
    assert not (engine.SPOKEN / history[1].name).exists()


def test_history_voice_rejection_restores_the_history_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_voice_rejection")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart"}
    history = [
        engine.SPOKEN / "002-af_heart-say.txt",
        engine.SPOKEN / "001-af_heart-say.txt",
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order(history)

    request_id = engine.request_play(history[-1].stem, "bm_fable")
    assert engine.process_play_request(queue.Queue(), engine.State()) is None

    with pytest.raises(RuntimeError, match="could not replay"):
        engine.wait_for_play_ack(request_id, timeout=0.1)
    assert engine.history_files_in_order() == history
    assert not list(engine.QUEUE.glob("*.txt"))


def test_changing_a_waiting_voice_keeps_the_selection_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_waiting_voice")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    older = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-bm_fable-g500-say.txt"
    newer = engine.QUEUE / "003-bm_fable-say.txt"
    older.write_text("Older", encoding="utf-8")
    selected.write_text("Selected words", encoding="utf-8")
    newer.write_text("Newer", encoding="utf-8")
    state = engine.State()

    request_id = engine.request_play(selected.stem, "af_heart")
    assert engine.process_play_request(queue.Queue(), state) == "select"
    acceptance = engine.wait_for_play_ack(request_id, timeout=0.1)
    variant = engine.QUEUE / f"{acceptance['id']}.txt"

    assert variant.name == "002-af_heart-g500-say.txt"
    assert variant.read_text(encoding="utf-8") == "Selected words"
    assert not older.exists()
    assert not selected.exists()
    assert (engine.SPOKEN / older.name).exists()
    assert not (engine.SPOKEN / selected.name).exists()
    assert engine.claim_next_queued_chunk(state) == variant
    assert engine.claim_next_queued_chunk(state) == newer


def test_changing_the_current_voice_replaces_it_without_changing_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_current_voice")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    current = engine.QUEUE / "001-af_heart-g600-say.txt"
    current.write_text("Current words", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    state.claimed.add(current.name)

    request_id = engine.request_play(current.stem, "bm_fable")
    assert engine.process_play_request(queue.Queue(), state) == "select"
    acceptance = engine.wait_for_play_ack(request_id, timeout=0.1)
    variant = engine.QUEUE / f"{acceptance['id']}.txt"

    assert variant.name == "001-bm_fable-g600-say.txt"
    assert variant.read_text(encoding="utf-8") == "Current words"
    assert not current.exists()
    assert not (engine.SPOKEN / current.name).exists()
    assert engine.claim_next_queued_chunk(state) == variant


def test_play_ack_reports_a_missing_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_missing")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    request_id = engine.request_play("999-af_heart-say")
    assert engine.process_play_request(queue.Queue(), engine.State()) is None

    with pytest.raises(RuntimeError, match="chunk not found"):
        engine.wait_for_play_ack(request_id, timeout=0.1)


def test_engine_loop_replays_history_before_the_existing_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_loop_replay")
    configure_runtime(engine, tmp_path)
    for name in ("INTERRUPT", "SKIP", "CLEAR", "WARMUP", "HEARTBEAT"):
        setattr(engine, name, tmp_path / name)
    engine.MODEL_DIR = tmp_path / "models"
    engine.MODEL_PATH = engine.MODEL_DIR / "model.onnx"
    engine.VOICES_PATH = engine.MODEL_DIR / "voices.bin"
    engine.MODEL_DIR.mkdir()
    engine.MODEL_PATH.touch()
    engine.VOICES_PATH.touch()
    engine.POLL_INTERVAL = 0.01
    engine.SIGNAL_TICK = 0.001
    engine.CHUNK_GAP_S = 0
    engine.SILENT = False

    queued = engine.QUEUE / "001-af_heart-say.txt"
    archived = engine.SPOKEN / "007-bm_fable-say.txt"
    queued.write_text("First queued", encoding="utf-8")
    archived.write_text("Replay me", encoding="utf-8")
    play_request = engine.BASE / f"PLAY.1.{'a' * 24}.json"
    play_request.write_text(
        json.dumps({"id": archived.stem, "request_id": "a" * 24}), encoding="utf-8"
    )

    played_samples: list[int] = []

    class FakeCallbackStop(Exception):
        pass

    class FakeOutputStream:
        def __init__(self, *, callback, finished_callback, **_kwargs) -> None:
            self.callback = callback
            self.finished_callback = finished_callback
            self.active = False

        def start(self) -> None:
            self.active = True
            playback = self.callback.__self__
            played_samples.append(int(playback.audio[0, 0]))
            output = np.empty_like(playback.audio)
            try:
                self.callback(output, len(output), None, None)
            except FakeCallbackStop:
                pass
            self.active = False
            self.finished_callback()
            if len(played_samples) == 2:
                engine.STOP.touch()

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            self.active = False

    class FakeKokoro:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @classmethod
        def from_session(cls, *_args, **_kwargs):
            return cls()

        def get_voices(self) -> list[str]:
            return ["af_bella", "af_heart", "bm_fable"]

        def create(self, text: str, **_kwargs):
            sample = {"Replay me": 8, "First queued": 1}.get(text, 0)
            return np.full(4, sample, dtype=np.float32), 1000

    class FakeSessionOptions:
        intra_op_num_threads = 0

    fake_sounddevice = SimpleNamespace(
        CallbackStop=FakeCallbackStop,
        OutputStream=FakeOutputStream,
        play=lambda *_args, **_kwargs: None,
        wait=lambda: None,
    )
    fake_onnxruntime = SimpleNamespace(
        SessionOptions=FakeSessionOptions,
        InferenceSession=lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=FakeKokoro))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_onnxruntime)

    engine.run_engine_loop()

    assert played_samples == [8, 1]
    assert archived.read_text(encoding="utf-8") == "Replay me"
    assert (engine.SPOKEN / queued.name).read_text(encoding="utf-8") == "First queued"
    assert len(list(engine.SPOKEN.glob("*.txt"))) == 2
    assert not list(engine.QUEUE.glob("*.txt"))
    final_status = json.loads(engine.STATUS.read_text(encoding="utf-8"))
    assert final_status["recent_starts"][0]["id"] == queued.stem


def test_idle_selection_replaces_worker_claims_and_becomes_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_idle_claimed")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    earlier = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    earlier.write_text("Earlier", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()
    state.claimed.add(earlier.name)

    engine.request_play(selected.stem)
    assert engine.process_play_request(queue.Queue(), state) == "select"

    assert state.playing == selected.name
    assert state.claimed == set()
    assert engine.claim_next_queued_chunk(state) == selected


def test_selection_interrupts_an_inter_chunk_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_gap")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()

    engine.request_play(selected.stem)

    assert engine.gap_wait(1.0, queue.Queue(), state) == "select"
    assert engine.claim_next_queued_chunk(state) == selected


def test_reordering_during_a_gap_discards_the_held_piece(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_gap")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    held = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    held.write_text("Held", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()
    state.claimed.add(held.name)
    request_id = engine.request_queue_command("move", selected.stem, held.stem)

    assert engine.gap_wait(1.0, queue.Queue(), state) == "queue_changed"
    engine.wait_for_queue_ack(request_id, timeout=0.1)

    assert held.is_file()
    assert state.claimed == set()
    assert engine.claim_next_queued_chunk(state) == selected


@pytest.mark.parametrize("outcome", ["done", "skip"])
def test_failed_archive_keeps_chunk_claimed_instead_of_repeating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    engine = load_engine("super_speech_engine_archive_failure")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    state.claimed.add(current.name)
    monkeypatch.setattr(engine, "archive", lambda path: False)
    monkeypatch.setattr(engine, "archive_failed", lambda path: False)

    assert engine.finish_chunk_playback(current, outcome, True, state)

    assert state.playing is None
    assert state.claimed == {current.name}
    assert state.stop.is_set()
    assert engine.claim_next_queued_chunk(state) is None


def test_failed_clear_archive_stops_instead_of_leaving_a_stuck_waiting_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_clear_archive_failure")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    state.claimed.add(waiting.name)
    monkeypatch.setattr(engine, "drop_to_spoken", lambda _path: False)

    engine.do_clear(queue.Queue(), state)

    assert waiting.exists()
    assert state.claimed == {waiting.name}
    assert state.stop.is_set()


def test_clear_archives_each_buffered_chunk_only_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_clear_unique_paths")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    state.claimed.add(waiting.name)
    archived: list[str] = []
    real_drop = engine.drop_to_spoken

    def observe_archive(path: Path) -> bool:
        archived.append(path.name)
        return real_drop(path)

    monkeypatch.setattr(engine, "drop_to_spoken", observe_archive)
    buffer: queue.Queue = queue.Queue()
    buffer.put((waiting,))
    buffer.put((waiting,))

    engine.do_clear(buffer, state)

    assert archived == [waiting.name]
    assert not state.stop.is_set()
    assert state.claimed == set()
    assert (engine.SPOKEN / waiting.name).exists()


def test_failed_synthesis_archive_stops_instead_of_leaving_a_stuck_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_synth_archive_failure")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()

    class FailingKokoro:
        @staticmethod
        def create(*_args, **_kwargs):
            raise RuntimeError("synthesis failed")

    monkeypatch.setattr(engine, "archive_failed", lambda _path: False)
    worker = threading.Thread(
        target=engine.synth_worker,
        args=(FailingKokoro(), queue.Queue(), state),
    )
    worker.start()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert waiting.exists()
    assert state.claimed == {waiting.name}
    assert state.stop.is_set()


@pytest.mark.parametrize("failure", ["empty", "synthesis"])
def test_preplay_terminal_item_releases_the_current_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    engine = load_engine(f"super_speech_engine_preplay_release_{failure}")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("" if failure == "empty" else "Waiting", encoding="utf-8")
    state = engine.State()
    state.playing = waiting.name

    class Kokoro:
        @staticmethod
        def create(*_args, **_kwargs):
            raise RuntimeError("synthesis failed")

    worker = threading.Thread(
        target=engine.synth_worker,
        args=(Kokoro(), queue.Queue(), state),
    )
    worker.start()
    destination = engine.SPOKEN if failure == "empty" else engine.FAILED
    for _ in range(100):
        if (destination / waiting.name).exists():
            break
        threading.Event().wait(0.01)
    state.stop.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert state.playing is None
    assert state.claimed == set()


def test_transient_current_read_failure_does_not_block_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_current_read_failure")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    state.playing = waiting.name
    original_read_text = Path.read_text
    read_failed = threading.Event()
    release_failure = threading.Event()

    def fail_once(path: Path, *args, **kwargs) -> str:
        if path == waiting and not read_failed.is_set():
            read_failed.set()
            assert release_failure.wait(1)
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_once)
    worker = threading.Thread(
        target=engine.synth_worker,
        args=(SimpleNamespace(), queue.Queue(), state),
    )
    worker.start()
    assert read_failed.wait(1)
    state.saw_stop = True
    release_failure.set()
    for _ in range(100):
        if state.playing is None:
            break
        threading.Event().wait(0.01)
    state.stop.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert waiting.exists()
    assert state.playing is None


def test_stop_during_a_gap_keeps_the_next_chunk_queued(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_stop_gap")
    configure_runtime(engine, tmp_path)
    next_chunk = engine.QUEUE / "002-bm_fable-say.txt"
    next_chunk.write_text("Next", encoding="utf-8")
    state = engine.State()
    state.claimed.add(next_chunk.name)
    engine.STOP.touch()

    assert engine.gap_wait(1.0, queue.Queue(), state) == "stop"
    assert next_chunk.is_file()


def test_fatal_engine_stop_ends_a_gap_before_the_next_chunk(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_fatal_gap")
    configure_runtime(engine, tmp_path)
    state = engine.State()
    state.stop.set()

    assert engine.gap_wait(1.0, queue.Queue(), state) == "fatal"


def test_new_speech_cancels_a_pending_graceful_stop(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_continue_gap")
    configure_runtime(engine, tmp_path)
    engine.SIGNAL_TICK = 0.001
    state = engine.State()
    state.saw_stop = True
    engine.STOP.touch()
    engine.CONTINUE.touch()

    assert engine.gap_wait(0.001, queue.Queue(), state) is None
    assert not state.saw_stop
    assert not engine.STOP.exists()


def test_speak_waits_for_engine_acceptance_after_queueing_new_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_speak_continue")
    configure_runtime(engine, tmp_path)
    starts: list[str] = []
    monkeypatch.setattr(engine, "start_engine", lambda: starts.append("start"))
    monkeypatch.setattr(
        engine,
        "enqueue_text",
        lambda _text, _voice, _gap: engine.QUEUE / "001-af_heart-say.txt",
    )
    monkeypatch.setattr(
        engine,
        "wait_for_queue_acceptance",
        lambda: starts.append("accept") or True,
    )

    assert engine.cli(["speak", "New work"]) == 0
    assert starts == ["start", "accept"]
    assert engine.CONTINUE.exists()


def test_speak_reports_durable_queueing_when_engine_cannot_accept_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = load_engine("super_speech_engine_speak_queued_later")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "start_engine", lambda: None)
    monkeypatch.setattr(engine, "wait_for_queue_acceptance", lambda: False)

    assert engine.cli(["speak", "New work"]) == 0

    captured = capsys.readouterr()
    assert "speech remains queued; playback will begin when the engine is ready" in captured.err
    assert Path(captured.out.strip()).read_text(encoding="utf-8") == "New work"


def test_queue_acceptance_waits_until_the_engine_consumes_continue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_acceptance")
    configure_runtime(engine, tmp_path)
    engine.CONTINUE.touch()
    starts = []

    def start() -> None:
        starts.append("start")
        engine.CONTINUE.unlink()

    monkeypatch.setattr(engine, "start_engine", start)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    assert engine.wait_for_queue_acceptance(timeout=0.1)

    assert starts == ["start"]


def test_queue_acceptance_does_not_fail_while_engine_is_still_loading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_acceptance_loading")
    configure_runtime(engine, tmp_path)
    engine.CONTINUE.touch()
    starts = 0

    def start() -> None:
        nonlocal starts
        starts += 1
        if starts == 3:
            engine.CONTINUE.unlink()

    monkeypatch.setattr(engine, "start_engine", start)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    assert engine.wait_for_queue_acceptance(timeout=0.2)

    assert starts == 3


def test_queue_acceptance_failure_reports_durable_work_as_queued(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_acceptance_failed")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Speak later", encoding="utf-8")
    engine.CONTINUE.touch()

    def fail_start() -> None:
        raise RuntimeError("engine failed")

    monkeypatch.setattr(engine, "start_engine", fail_start)
    monkeypatch.setattr(engine, "engine_is_running", lambda: False)

    assert not engine.wait_for_queue_acceptance(timeout=0.01)
    assert waiting.read_text(encoding="utf-8") == "Speak later"


def test_clear_during_a_gap_does_not_play_the_archived_chunk(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_gap")
    configure_runtime(engine, tmp_path)
    next_chunk = engine.QUEUE / "002-bm_fable-say.txt"
    next_chunk.write_text("Next", encoding="utf-8")
    state = engine.State()
    state.claimed.add(next_chunk.name)
    engine.CLEAR.touch()

    assert engine.gap_wait(1.0, queue.Queue(), state) == "clear"
    assert not next_chunk.exists()
    assert (engine.SPOKEN / next_chunk.name).read_text(encoding="utf-8") == "Next"


def test_clear_cannot_race_a_worker_refilling_a_full_buffer(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_full_buffer")
    configure_runtime(engine, tmp_path)
    engine.SIGNAL_TICK = 0.001
    current = engine.QUEUE / "001-af_heart-say.txt"
    queued = engine.QUEUE / "002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    queued.write_text("Queued", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    state.claimed.add(current.name)

    class ObservedFullBuffer:
        def __init__(self, entry: tuple[object, ...]) -> None:
            self.items = [entry]
            self.worker_attempted = threading.Event()
            self.space_observed = threading.Event()
            self.worker_inserted = threading.Event()

        def put(
            self,
            entry: tuple[object, ...],
            block: bool = True,
            timeout: float | None = None,
        ) -> None:
            if threading.current_thread() is not threading.main_thread():
                self.worker_attempted.set()
                if block:
                    self.space_observed.wait(timeout)
            if self.items:
                raise queue.Full
            self.items.append(entry)
            if threading.current_thread() is not threading.main_thread():
                self.worker_inserted.set()

        def put_nowait(self, entry: tuple[object, ...]) -> None:
            self.put(entry, block=False)

        def get_nowait(self) -> tuple[object, ...]:
            if self.items:
                return self.items.pop(0)
            self.space_observed.set()
            self.worker_inserted.wait(0.05)
            raise queue.Empty

    current_entry = (
        current,
        np.zeros(1, dtype=np.float32),
        1000,
        False,
        True,
        2,
        2,
        "Current",
        "af_heart",
    )
    buffer = ObservedFullBuffer(current_entry)
    kokoro = SimpleNamespace(
        create=lambda *_args, **_kwargs: (np.zeros(1, dtype=np.float32), 1000)
    )
    worker = threading.Thread(
        target=engine.synth_worker, args=(kokoro, buffer, state), daemon=True
    )
    worker.start()
    assert buffer.worker_attempted.wait(1)

    engine.do_clear(buffer, state)
    state.stop.set()
    worker.join(1)

    assert buffer.get_nowait() is current_entry
    assert not worker.is_alive()
    assert state.claimed == {current.name}
    assert (engine.SPOKEN / queued.name).read_text(encoding="utf-8") == "Queued"


def test_startup_cleanup_preserves_unclaimed_requests(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_play")
    configure_runtime(engine, tmp_path)
    engine.PLAY.write_text('{"id":"001-af_heart-say"}', encoding="utf-8")
    request = engine.BASE / f"PLAY.1.{'a' * 24}.json"
    request.write_text("{}", encoding="utf-8")
    claim = request.with_suffix(".claim")
    claim.write_text("{}", encoding="utf-8")
    temporary = engine.BASE / f"PLAY.1.{'a' * 24}.json.1.1.tmp"
    temporary.write_text("{}", encoding="utf-8")
    acknowledgement = engine.BASE / f"PLAY_ACK.{'a' * 24}.json"
    acknowledgement.write_text("{}", encoding="utf-8")
    os.utime(acknowledgement, (0, 0))
    queue_request = engine.BASE / f"QUEUE_COMMAND.1.{'b' * 24}.json"
    queue_request.write_text("{}", encoding="utf-8")
    queue_acknowledgement = engine.BASE / f"QUEUE_ACK.{'b' * 24}.json"
    queue_acknowledgement.write_text("{}", encoding="utf-8")
    os.utime(queue_acknowledgement, (0, 0))

    engine.clear_transient_signals()

    assert not engine.PLAY.exists()
    assert request.exists()
    assert not claim.exists()
    assert not temporary.exists()
    assert not acknowledgement.exists()
    assert queue_request.exists()
    assert not queue_acknowledgement.exists()


def test_startup_rejects_an_abandoned_claim_without_leaving_its_caller_waiting(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_recover_claim")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    claim = engine.BASE / f"PLAY.1.{request_id}.claim"
    claim.write_text(
        json.dumps(
            {
                "id": "001-af_heart-say",
                "voice": None,
                "request_id": request_id,
            }
        ),
        encoding="utf-8",
    )

    engine.clear_transient_signals()

    with pytest.raises(RuntimeError, match="engine restarted"):
        engine.wait_for_play_ack(request_id, timeout=0.1)
    assert not claim.exists()


def test_failed_ack_keeps_the_applied_claim_without_repeating_the_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_applied_claim")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    history = engine.SPOKEN / "001-af_heart-say.txt"
    history.write_text("Delete once", encoding="utf-8")
    request_id = engine.request_queue_command("delete", history.stem)
    monkeypatch.setattr(engine, "publish_queue_ack", lambda *_args, **_kwargs: False)

    assert engine.process_queue_requests(queue.Queue(), engine.State())
    claims = list(engine.BASE.glob(f"QUEUE_COMMAND.*.{request_id}.claim"))

    assert len(claims) == 1
    assert not history.exists()
    assert not engine.process_queue_requests(queue.Queue(), engine.State())
    with pytest.raises(RuntimeError, match="result was unconfirmed"):
        engine.wait_for_queue_ack(request_id, timeout=0.01)


def test_claimed_request_returns_unconfirmed_when_successor_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_claim_restart_failure")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    claim = engine.BASE / f"QUEUE_COMMAND.1.{request_id}.claim"
    claim.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(engine, "engine_is_running", lambda: False)

    def fail_start() -> None:
        raise RuntimeError("forced startup failure")

    monkeypatch.setattr(engine, "start_engine", fail_start)

    with pytest.raises(RuntimeError, match="result was unconfirmed"):
        engine.wait_for_queue_ack(request_id, timeout=0.01)

    assert claim.exists()


def test_play_ack_pruning_keeps_active_waiters(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_ack_pruning")
    configure_runtime(engine, tmp_path)
    stale = engine.BASE / f"PLAY_ACK.{'a' * 24}.json"
    active = engine.BASE / f"PLAY_ACK.{'b' * 24}.json"
    stale.write_text("{}", encoding="utf-8")
    active.write_text("{}", encoding="utf-8")
    os.utime(stale, (0, 0))

    engine.prune_play_acknowledgements()

    assert not stale.exists()
    assert active.exists()


def test_serve_removes_stale_status_after_acquiring_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_serve_status")
    configure_runtime(engine, tmp_path)
    engine.STATUS.write_text("stale", encoding="utf-8")
    observed: list[bool] = []

    class FakeLock:
        @staticmethod
        def acquire() -> bool:
            return True

        @staticmethod
        def release() -> None:
            pass

    monkeypatch.setattr(engine, "EngineInstanceLock", FakeLock)
    monkeypatch.setattr(
        engine, "run_engine_loop", lambda: observed.append(engine.STATUS.exists())
    )

    engine.serve()

    assert observed == [False]


def test_status_exposes_bounded_recent_history(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_status")
    configure_runtime(engine, tmp_path)
    engine.HISTORY_LIMIT = 2
    for number in (1, 2, 3):
        (engine.SPOKEN / f"{number:03d}-af_heart-say.txt").write_text(
            f"History {number}", encoding="utf-8"
        )

    engine.publish_status("idle", engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["version"] == engine.STATUS_VERSION
    assert status["history_count"] == 3
    assert [item["text"] for item in status["history"]] == ["History 3", "History 2"]


def test_history_reordering_is_persisted_within_the_recent_window(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_order")
    configure_runtime(engine, tmp_path)
    engine.HISTORY_LIMIT = 2
    first = engine.SPOKEN / "001-af_heart-say.txt"
    second = engine.SPOKEN / "002-af_heart-say.txt"
    third = engine.SPOKEN / "003-af_heart-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")

    engine.apply_queue_command(queue.Queue(), engine.State(), "move_history", third.stem, None)

    assert engine.history_files_in_order() == [second, third, first]
    assert [item["id"] for item in engine.history_snapshot()[1]] == [
        second.stem,
        third.stem,
    ]


def test_new_history_item_stays_newest_after_manual_reordering(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_newest")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-af_heart-say.txt"
    second = engine.SPOKEN / "002-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    engine.save_history_order([first, second])
    newest = engine.QUEUE / "003-af_heart-say.txt"
    newest.write_text("Newest", encoding="utf-8")

    assert engine.archive(newest)

    assert engine.history_files_in_order() == [engine.SPOKEN / newest.name, first, second]


def test_new_archive_and_history_reorder_commit_in_one_serial_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_serial_order")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-af_heart-say.txt"
    second = engine.SPOKEN / "002-af_heart-say.txt"
    newest = engine.QUEUE / "003-af_heart-say.txt"
    for path in (first, second, newest):
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order([first, second])
    entered_save = threading.Event()
    release_save = threading.Event()
    real_save = engine.save_history_order

    def pause_reorder_save(paths=None) -> None:
        if threading.current_thread().name == "history-reorder":
            entered_save.set()
            assert release_save.wait(1)
        real_save(paths)

    monkeypatch.setattr(engine, "save_history_order", pause_reorder_save)
    reorder = threading.Thread(
        name="history-reorder",
        target=engine.apply_queue_command,
        args=(queue.Queue(), engine.State(), "move_history", second.stem, first.stem),
    )
    archive = threading.Thread(target=engine.archive, args=(newest,))
    reorder.start()
    assert entered_save.wait(1)
    archive.start()
    assert archive.is_alive()
    release_save.set()
    reorder.join(1)
    archive.join(1)

    assert not reorder.is_alive()
    assert not archive.is_alive()
    assert engine.history_files_in_order() == [engine.SPOKEN / newest.name, second, first]


def test_history_snapshot_and_archive_cannot_deadlock_each_other(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_lock_order")
    configure_runtime(engine, tmp_path)
    archived = engine.SPOKEN / "001-af_heart-say.txt"
    queued = engine.QUEUE / "002-af_heart-say.txt"
    archived.write_text("Earlier", encoding="utf-8")
    queued.write_text("New", encoding="utf-8")
    archive_holds_order = threading.Event()
    snapshot_reading = threading.Event()
    real_save = engine.save_history_order
    real_history_files = engine.history_files_in_order

    def gated_save(paths=None) -> None:
        if threading.current_thread().name == "archive":
            archive_holds_order.set()
            snapshot_reading.wait(0.1)
        real_save(paths)

    def observed_history_files() -> list[Path]:
        if threading.current_thread().name == "snapshot":
            snapshot_reading.set()
        return real_history_files()

    monkeypatch.setattr(engine, "save_history_order", gated_save)
    monkeypatch.setattr(engine, "history_files_in_order", observed_history_files)
    archive_thread = threading.Thread(
        name="archive", target=engine.archive, args=(queued,), daemon=True
    )
    snapshot_thread = threading.Thread(
        name="snapshot", target=engine.history_snapshot, daemon=True
    )

    archive_thread.start()
    assert archive_holds_order.wait(1)
    snapshot_thread.start()
    archive_thread.join(1)
    snapshot_thread.join(1)

    assert not archive_thread.is_alive()
    assert not snapshot_thread.is_alive()


def test_history_orders_legacy_suffixed_ids_by_their_leading_sequence(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_history_legacy_order")
    configure_runtime(engine, tmp_path)
    for name in (
        "8552a-af_bella-say.txt",
        "10223-af_heart-say.txt",
        "10224-af_heart-say.txt",
    ):
        (engine.SPOKEN / name).write_text(name, encoding="utf-8")

    _, history = engine.history_snapshot()

    assert [item["id"].split("-", 1)[0] for item in history] == [
        "10224",
        "10223",
        "8552a",
    ]


def test_history_snapshot_refreshes_only_after_an_archive_move(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_cache")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")

    first_count, first_items = engine.history_snapshot()
    cached_count, cached_items = engine.history_snapshot()

    assert first_count == cached_count == 1
    assert cached_items is first_items

    second = engine.QUEUE / "002-bm_fable-say.txt"
    second.write_text("Second", encoding="utf-8")
    assert engine.archive(second)
    refreshed_count, refreshed_items = engine.history_snapshot()

    assert refreshed_count == 2
    assert refreshed_items is not first_items
    assert [item["text"] for item in refreshed_items] == ["Second", "First"]
