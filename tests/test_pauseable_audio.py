from __future__ import annotations

import importlib.util
import json
import os
import queue
import sys
import threading
from dataclasses import FrozenInstanceError
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
    engine.MUTATION = tmp_path / "MUTATION.json"
    engine.QUEUE_ORDER = tmp_path / "queue-order.json"
    engine.HISTORY_ORDER = tmp_path / "history-order.json"
    engine.WARMUP = tmp_path / "WARMUP"
    engine.HEARTBEAT = tmp_path / "engine.alive"
    engine.STATUS = tmp_path / "status.json"
    engine.STATUS_FAILURE = tmp_path / "status.failed"
    engine.INSTANCE_LOCK = tmp_path / "engine.lock"
    engine.TIMELINE_LOCK = tmp_path / "timeline.lock"
    engine.TIMELINE_INTENT = tmp_path / "timeline-intent.json"
    engine.IDENTITY_INDEX = tmp_path / "speechicle-index.json"
    engine._identity_cache_path = None
    engine._identity_cache = None
    engine._identity_ready_path = None
    engine.QUEUE.mkdir(exist_ok=True)
    engine.SPOKEN.mkdir(exist_ok=True)


def speechicle_id(engine, path: Path) -> str:
    engine.ensure_identity_catalog()
    return engine.public_id_for_path(path)


def request_mutation(engine, mutation_type: str, **fields: object) -> str:
    return engine.request_mutation(
        engine.build_mutation_request(mutation_type, **fields)
    )


def committed_result(engine, request_id: str) -> dict[str, object]:
    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "committed"
    return result


def rejected_result(engine, request_id: str) -> dict[str, object]:
    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "rejected"
    return result


def test_split_text_pieces_retains_source_ranges_with_unicode_and_spacing() -> None:
    engine = load_engine("super_speech_engine_piece_ranges")
    text = "First 😀 sentence.\n\nSecond sentence?   Third sentence."

    pieces = engine.split_text_pieces(text, 22)

    assert [piece.text for piece in pieces] == [
        "First 😀 sentence.",
        "Second sentence?",
        "Third sentence.",
    ]
    assert [text[piece.start : piece.end] for piece in pieces] == [
        "First 😀 sentence.",
        "Second sentence?",
        "Third sentence.",
    ]


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

    engine.publish_status("playing", state, force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "paused"
    assert status["current"]["text"] == "Current words"
    assert status["current"]["voice"] == "af_heart"
    assert status["timeline_revision"] == 0
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
    assert status["current"]["id"] == speechicle_id(engine, current)


def test_audio_stream_failure_propagates_before_playback(
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
            _, generation = engine._record_claim(state, queued)
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
                0,
                len("Keep visible"),
                generation,
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
    assert status["current"]["id"] == speechicle_id(engine, queued)
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
    assert status["current"]["id"] == speechicle_id(engine, queued)
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

    assert status["current"]["id"] == speechicle_id(engine, readable)
    assert status["queue_count"] == len(status["queue"]) == 1
    assert status["queue"][0]["id"] == speechicle_id(engine, unreadable)
    assert status["queue"][0]["text"] == ""


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


def test_persistent_status_publication_failure_stops_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status_write_failure")
    configure_runtime(engine, tmp_path)
    engine.ensure_identity_catalog()
    state = engine.State()
    engine._status_failure_started = engine.time.time() - 6
    real_replace = engine.os.replace

    def fail_replace(*_args) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(engine.os, "replace", fail_replace)

    engine.publish_status("idle", state, force=True)

    assert state.stop.is_set()
    assert engine.STATUS_FAILURE.exists()

    monkeypatch.setattr(engine.os, "replace", real_replace)
    state.stop.clear()
    engine.publish_status("idle", state, force=True)

    assert not engine.STATUS_FAILURE.exists()


def test_status_publication_uses_a_fresh_temporary_file_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status_fresh_temporary")
    configure_runtime(engine, tmp_path)
    engine.ensure_identity_catalog()
    state = engine.State()
    temporary_paths: list[Path] = []
    real_replace = engine.os.replace

    def replace(source: Path, target: Path) -> None:
        temporary_paths.append(Path(source))
        if len(temporary_paths) == 1:
            raise PermissionError("temporary file is being scanned")
        real_replace(source, target)

    monkeypatch.setattr(engine.os, "replace", replace)

    engine.publish_status("idle", state, force=True)
    engine.publish_status("idle", state, force=True)

    assert len(temporary_paths) == 2
    assert temporary_paths[0] != temporary_paths[1]
    assert not state.stop.is_set()
    assert json.loads(engine.STATUS.read_text(encoding="utf-8"))["state"] == "idle"
    log = engine.LOG.read_text(encoding="utf-8")
    assert "status publication failed: PermissionError" in log
    assert "status publication recovered" in log


def test_stopped_status_contains_the_same_queue_items_as_its_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = load_engine("super_speech_engine_stopped_status")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")

    engine.print_status()
    status = json.loads(capsys.readouterr().out)

    assert status["current"]["id"] == speechicle_id(engine, waiting)
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
    new_arrival = engine.enqueue_text("New", "bm_george")

    assert [path.stem for path in engine.queue_files_in_order()] == [
        third.stem,
        first.stem,
        second.stem,
        new_arrival.stem,
    ]


def test_queue_order_uses_its_cache_during_a_transient_sidecar_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_order_lock")
    configure_runtime(engine, tmp_path)
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    engine.save_queue_order([second, first])
    original_read_text = Path.read_text

    def locked_order(path: Path, *args, **kwargs) -> str:
        if path == engine.QUEUE_ORDER:
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked_order)

    assert engine.claim_next_queued_chunk(engine.State()) == second


def test_voice_rename_recovery_keeps_the_saved_queue_position(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_voice_rename_recovery")
    configure_runtime(engine, tmp_path)
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-af_heart-say.txt"
    third = engine.QUEUE / "003-af_heart-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")
    engine.save_queue_order([third, first, second])
    renamed = engine.QUEUE / "001-bm_fable-say.txt"
    os.replace(first, renamed)

    assert engine.queue_files_in_order() == [third, renamed, second]


def test_failed_sequence_numbers_are_not_reused(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_failed_sequence")
    configure_runtime(engine, tmp_path)
    engine.FAILED.mkdir()
    (engine.FAILED / "007-af_heart-say.txt").write_text("Failed", encoding="utf-8")

    assert engine.enqueue_text("New", "af_heart").name == "008-af_heart-say.txt"


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
    generations: dict[str, int] = {}
    for path in (current, second, third):
        _, generations[path.name] = engine._record_claim(state, path)

    engine.apply_queue_command(
        buffered,
        state,
        "move",
        speechicle_id(engine, third),
        speechicle_id(engine, second),
    )

    assert [path.stem for path in engine.queue_files_in_order()] == [
        current.stem,
        third.stem,
        second.stem,
    ]
    assert buffered.get_nowait() == (current, "current piece")
    assert buffered.empty()
    assert state.claims == {current.name: generations[current.name]}
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
    for path in (current, archived, remaining):
        engine._record_claim(state, path)

    engine.apply_queue_command(
        buffered, state, "archive", speechicle_id(engine, archived), None
    )

    assert not archived.exists()
    assert (engine.SPOKEN / archived.name).is_file()
    assert [path.stem for path in engine.queue_files_in_order()] == [
        current.stem,
        remaining.stem,
    ]
    assert buffered.get_nowait() == (current, "current piece")
    assert set(state.claims) == {current.name}


def test_queue_mutation_cannot_cancel_an_accepted_selection(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_selected_queue_mutation")
    configure_runtime(engine, tmp_path)
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()
    state.selection_name = selected.name

    with pytest.raises(ValueError, match="selected speech is starting"):
        engine.apply_queue_command(
            queue.Queue(), state, "archive", speechicle_id(engine, selected), None
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

    engine.apply_queue_command(
        queue.Queue(), engine.State(), "delete", speechicle_id(engine, history), None
    )

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
        f"sp_{'1' * 32}",
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
            queue.Queue(),
            engine.State(),
            "delete",
            speechicle_id(engine, queued),
            None,
        )

    assert queued.exists()
    assert not history.exists()


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

    engine.apply_queue_command(
        queue.Queue(), engine.State(), "delete", speechicle_id(engine, history), None
    )

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


def test_archive_waits_for_history_order_before_moving_the_current_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_order_fallback")
    configure_runtime(engine, tmp_path)
    older = engine.SPOKEN / "001-af_heart-say.txt"
    newer = engine.SPOKEN / "002-af_heart-say.txt"
    current = engine.QUEUE / "003-af_heart-say.txt"
    for path in (older, newer, current):
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order([newer, older])

    real_write = engine._write_saved_order

    def fail_history_order(path: Path, ids: list[str]) -> None:
        if path == engine.HISTORY_ORDER:
            raise PermissionError("locked")
        real_write(path, ids)

    monkeypatch.setattr(engine, "_write_saved_order", fail_history_order)

    assert not engine.archive(current)
    assert current.exists()
    assert engine.history_files_in_order() == [newer, older]


def test_waiting_move_publishes_a_committed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_request")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-bm_fable-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")

    request_id = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, second),
        before_id=speechicle_id(engine, first),
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) == "queue_changed"
    committed_result(engine, request_id)

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
    request_id = request_mutation(
        engine, "archive", id=speechicle_id(engine, waiting)
    )

    with pytest.raises(RuntimeError, match="did not publish"):
        engine.wait_for_mutation_result(request_id, timeout=0.01)

    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None
    assert waiting.exists()


def test_timeout_retries_a_transient_request_cancellation_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_cancel_retry")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = request_mutation(engine, "delete", id=f"sp_{'1' * 32}")
    real_replace = engine.os.replace
    failures = 0

    def replace(source, destination) -> None:
        nonlocal failures
        if Path(source).name.endswith(f"{request_id}.json") and failures < 2:
            failures += 1
            raise PermissionError("temporarily locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", replace)

    with pytest.raises(RuntimeError, match="did not publish"):
        engine.wait_for_mutation_result(request_id, timeout=0.01)

    assert failures == 2
    assert not list(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))


def test_persistently_locked_unclaimed_request_returns_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_cancel_locked")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = request_mutation(engine, "delete", id=f"sp_{'1' * 32}")
    monkeypatch.setattr(engine, "cancel_unclaimed_mutation", lambda *_args: False)

    with pytest.raises(RuntimeError, match="result was unconfirmed"):
        engine.wait_for_mutation_result(request_id, timeout=0.01)

    assert list(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))


def test_result_published_at_the_timeout_boundary_is_still_observed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_result_boundary")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = request_mutation(engine, "delete", id=f"sp_{'1' * 32}")
    request = next(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))
    state = engine.State()
    snapshot = engine.publish_status("idle", state, force=True)
    assert snapshot is not None
    real_wait = engine.wait_for_json_payload
    calls = 0

    def wait(target: Path, deadline: float):
        nonlocal calls
        calls += 1
        if calls == 1:
            request.unlink()
            assert engine.publish_mutation_result(
                request_id, "committed", snapshot
            )
            return None
        return real_wait(target, deadline)

    monkeypatch.setattr(engine, "wait_for_json_payload", wait)

    committed_result(engine, request_id)

    assert calls == 2


def test_history_delete_publishes_a_committed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_delete_request")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    history = engine.SPOKEN / "001-af_heart-say.txt"
    history.write_text("History", encoding="utf-8")

    request_id = request_mutation(
        engine, "delete", id=speechicle_id(engine, history)
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None
    committed_result(engine, request_id)

    assert not history.exists()


def test_mutation_result_retries_a_transient_windows_read_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_result_retry")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    result_path = engine.mutation_result_path(request_id)
    snapshot = engine.publish_status("idle", engine.State(), force=True)
    assert snapshot is not None
    result_path.write_text(
        json.dumps(
            {
                "outcome": "committed",
                "request_id": request_id,
                "snapshot": snapshot,
            }
        ),
        encoding="utf-8",
    )
    original_read_text = Path.read_text
    attempts = 0

    def read_text(path: Path, *args, **kwargs) -> str:
        nonlocal attempts
        if path == result_path:
            attempts += 1
            if attempts == 1:
                raise PermissionError("result is being replaced")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    result = engine.wait_for_mutation_result(request_id, timeout=0.2)
    assert result["outcome"] == "committed"

    assert attempts == 2
    assert not result_path.exists()


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

    request_id = request_mutation(
        engine, "archive", id=speechicle_id(engine, current)
    )
    assert engine.process_mutation_requests(queue.Queue(), state) is None

    result = rejected_result(engine, request_id)
    assert "waiting chunk not found" in str(result["error"])
    assert current.is_file()


def test_unconfirmed_waiting_archive_stops_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_archive_unconfirmed")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    engine._record_claim(state, waiting)
    request_id = request_mutation(
        engine, "archive", id=speechicle_id(engine, waiting)
    )

    def fail_archive(_path: Path) -> bool:
        raise engine.MutationOutcomeUnconfirmed("rollback failed")

    monkeypatch.setattr(engine, "archive", fail_archive)

    assert engine.process_mutation_requests(queue.Queue(), state) is None
    assert state.stop.is_set()
    assert set(state.claims) == {waiting.name}
    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "unconfirmed"


def test_enqueue_text_ignores_a_stale_legacy_reservation(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_enqueue_race")
    configure_runtime(engine, tmp_path)
    (engine.QUEUE / "001.reserve").touch()

    queued = engine.enqueue_text("New words", "af_heart")

    assert queued.name == "001-af_heart-say.txt"


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
    public_id = f"sp_{'1' * 32}"
    monkeypatch.setattr(engine, "public_id_for_path", lambda _path: public_id)
    monkeypatch.setattr(
        engine, "wait_for_queue_acceptance", lambda: calls.append("accept")
    )

    assert engine.cli(["speak", "Hello there", "--gap-ms", "300"]) == 0
    assert calls == ["start", ("Hello there", "af_heart", 300), "accept"]
    assert capsys.readouterr().out.strip() == public_id


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


def test_interrupt_rejects_pending_mutations_with_authoritative_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_interrupt_requests")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    selected = engine.QUEUE / "001-af_heart-say.txt"
    selected.write_text("Selected", encoding="utf-8")
    selected_id = speechicle_id(engine, selected)
    play_request = request_mutation(engine, "play", id=selected_id, voice=None)
    move_request = request_mutation(
        engine,
        "move",
        section="waiting",
        id=selected_id,
        before_id=None,
    )
    engine.INTERRUPT.touch()

    assert engine.gap_wait(1.0, queue.Queue(), engine.State()) == "interrupt"
    play_result = rejected_result(engine, play_request)
    move_result = rejected_result(engine, move_request)
    assert "interrupted" in str(play_result["error"])
    assert "interrupted" in str(move_result["error"])


def test_play_command_starts_engine_then_publishes_the_requested_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_cli")
    configure_runtime(engine, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(engine, "start_engine", lambda: calls.append("start"))
    def request(mutation) -> str:
        calls.append((mutation.id, mutation.voice))
        return "a" * 24

    monkeypatch.setattr(engine, "request_mutation", request)
    public_id = f"sp_{'7' * 32}"
    monkeypatch.setattr(
        engine,
        "wait_for_mutation_result",
        lambda request_id: calls.append(("wait", request_id))
        or {"outcome": "committed", "request_id": request_id},
    )

    assert engine.cli(["play", public_id, "--voice", "af_heart"]) == 0
    assert calls == [
        "start",
        (public_id, "af_heart"),
        ("wait", "a" * 24),
    ]


def test_play_command_keeps_stdout_as_json_when_result_cleanup_is_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = load_engine("super_speech_engine_play_cli_locked_result")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    result_path = engine.mutation_result_path(request_id)
    public_id = f"sp_{'7' * 32}"
    snapshot = engine.publish_status("idle", engine.State(), force=True)
    assert snapshot is not None
    expected_result = {
        "outcome": "committed",
        "request_id": request_id,
        "result_id": public_id,
        "snapshot": snapshot,
    }
    result_path.write_text(
        json.dumps(
            expected_result
        ),
        encoding="utf-8",
    )
    real_unlink = Path.unlink

    def lock_result(path: Path, *args, **kwargs) -> None:
        if path == result_path:
            raise PermissionError("locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(engine, "start_engine", lambda: None)
    monkeypatch.setattr(engine, "request_mutation", lambda *_args: request_id)
    monkeypatch.setattr(Path, "unlink", lock_result)

    assert engine.cli(["play", public_id]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected_result
    assert "could not remove result" in captured.err


def test_play_request_rejects_a_non_id_before_touching_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_invalid")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    with pytest.raises(ValueError):
        request_mutation(engine, "play", id="../spoken/007", voice=None)

    assert not list(engine.BASE.glob("MUTATION.*.json"))


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
    engine._record_claim(state, current)
    state.saw_stop = True

    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, current), voice=None
    )
    assert engine.process_mutation_requests(buffered, state) is None
    result = committed_result(engine, request_id)

    assert not engine.PAUSE.exists()
    assert not engine.STOP.exists()
    assert not state.saw_stop
    assert set(state.claims) == {current.name}
    assert buffered.get_nowait() == "banked piece"
    assert result["result_id"] == speechicle_id(engine, current)


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
    for path in (current, older, selected):
        engine._record_claim(state, path)

    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(buffered, state) == "select"
    result = committed_result(engine, request_id)

    assert not current.exists()
    assert not older.exists()
    assert (engine.SPOKEN / current.name).read_text(encoding="utf-8") == "Current"
    assert (engine.SPOKEN / older.name).read_text(encoding="utf-8") == "Older waiting"
    assert state.claims == {}
    assert buffered.empty()

    assert engine.finish_chunk_playback(current, "select", False, state)
    assert state.playing == selected.name
    assert engine.claim_next_queued_chunk(state) == selected
    assert engine.claim_next_queued_chunk(state) == newer
    assert engine.claim_next_queued_chunk(state) is None
    assert result["result_id"] == speechicle_id(engine, selected)


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

    request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) == "select"

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
    monkeypatch.setattr(engine, "_archive_many", lambda _paths: False)
    state = engine.State()

    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) is None

    result = rejected_result(engine, request_id)
    assert "could not select" in str(result["error"])
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
    real_replace = engine.os.replace

    def fail_second(source, destination) -> None:
        if Path(source) == older[1] and Path(destination).parent == engine.SPOKEN:
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_second)
    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None

    result = rejected_result(engine, request_id)
    assert "could not select" in str(result["error"])
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
    real_replace = engine.os.replace

    def fail_second(source, destination) -> None:
        if Path(source) == older[1] and Path(destination).parent == engine.SPOKEN:
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_second)
    request_id = request_mutation(
        engine,
        "play",
        id=speechicle_id(engine, selected),
        voice="bm_fable",
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None

    result = rejected_result(engine, request_id)
    assert "could not select" in str(result["error"])
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

    archived_id = speechicle_id(engine, archived)
    request_id = request_mutation(
        engine, "play", id=archived_id, voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"

    replay = engine.QUEUE / archived.name
    assert not archived.exists()
    assert replay.read_text(encoding="utf-8") == "Say this again"
    assert engine.claim_next_queued_chunk(state) == replay
    assert committed_result(engine, request_id)["result_id"] == archived_id
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
            engine.enqueue_text("006-af_heart-say", "af_heart"),
            engine.enqueue_text("007-af_heart-say", "af_heart"),
        ]
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

    selected_id = speechicle_id(engine, selected)
    request_id = request_mutation(
        engine, "play", id=selected_id, voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    assert committed_result(engine, request_id)["result_id"] == selected_id
    selected_status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert selected_status["current"]["id"] == speechicle_id(engine, selected)
    assert visible_ids() == original_order

    restarted = engine.State()
    engine.publish_status("idle", restarted, force=True)
    assert visible_ids() == original_order
    assert json.loads(engine.STATUS.read_text(encoding="utf-8"))["current"][
        "id"
    ] == speechicle_id(engine, selected)

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

    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, archived), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) == "select"
    committed_result(engine, request_id)

    assert engine.claim_next_queued_chunk(engine.State()) == engine.QUEUE / archived.name


def test_startup_repairs_an_interrupted_history_boundary_move(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_boundary_recovery")
    configure_runtime(engine, tmp_path)
    history = [
        engine.SPOKEN / "003-af_heart-say.txt",
        engine.SPOKEN / "002-af_heart-say.txt",
        engine.SPOKEN / "001-af_heart-say.txt",
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order(history)
    os.replace(history[0], engine.QUEUE / history[0].name)
    os.replace(history[1], engine.QUEUE / history[1].name)

    engine.repair_interrupted_timeline_transition()

    assert engine.queue_files_in_order() == [
        engine.QUEUE / history[1].name,
        engine.QUEUE / history[0].name,
    ]
    assert engine.history_files_in_order() == [history[2]]


def test_startup_removes_legacy_active_history_duplicates(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_legacy_duplicate_repair")
    configure_runtime(engine, tmp_path)
    active = engine.QUEUE / "002-af_heart-say.txt"
    duplicate = engine.SPOKEN / active.name
    earlier = engine.SPOKEN / "001-af_heart-say.txt"
    active.write_text("Active", encoding="utf-8")
    duplicate.write_text("Active", encoding="utf-8")
    earlier.write_text("Earlier", encoding="utf-8")
    engine.save_history_order([duplicate, earlier])

    engine.repair_interrupted_timeline_transition()

    assert active.exists()
    assert not duplicate.exists()
    assert engine.history_files_in_order() == [earlier]


def test_startup_promotes_rows_above_a_legacy_history_replay(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_legacy_replay_boundary")
    configure_runtime(engine, tmp_path)
    newest = engine.SPOKEN / "003-af_heart-say.txt"
    middle = engine.SPOKEN / "002-af_heart-say.txt"
    selected_history = engine.SPOKEN / "001-af_heart-say.txt"
    selected_queue = engine.QUEUE / selected_history.name
    for path in (newest, middle, selected_history, selected_queue):
        path.write_text(path.stem, encoding="utf-8")
    engine.save_history_order([newest, middle, selected_history])

    engine.repair_interrupted_timeline_transition()

    assert engine.queue_files_in_order() == [
        selected_queue,
        engine.QUEUE / middle.name,
        engine.QUEUE / newest.name,
    ]
    assert engine.history_files_in_order() == []


def test_startup_removes_a_promotion_duplicate_backup(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_promotion_duplicate_recovery")
    configure_runtime(engine, tmp_path)
    duplicate_history = engine.SPOKEN / "003-af_heart-say.txt"
    duplicate_queue = engine.QUEUE / duplicate_history.name
    selected = engine.SPOKEN / "002-af_heart-say.txt"
    backup = engine.SPOKEN / f".{duplicate_history.name}.crash.duplicate"
    duplicate_history.write_text("Legacy History copy", encoding="utf-8")
    duplicate_queue.write_text("Active copy", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    engine._write_order_payload(engine.QUEUE_ORDER, [duplicate_queue.stem], 1)
    engine._write_order_payload(
        engine.HISTORY_ORDER, [duplicate_history.stem, selected.stem], 1
    )
    os.replace(duplicate_history, backup)
    engine._write_timeline_intent(
        {
            "version": 1,
            "operation": "promote",
            "moves": [
                {
                    "source": duplicate_history.name,
                    "target": duplicate_queue.name,
                    "backup": backup.name,
                },
                {
                    "source": selected.name,
                    "target": selected.name,
                    "backup": None,
                },
            ],
            "queue_ids": [selected.stem, duplicate_queue.stem],
            "history_ids": [],
        }
    )

    engine.repair_interrupted_timeline_transition()

    assert not backup.exists()
    assert duplicate_queue.read_text(encoding="utf-8") == "Active copy"
    assert engine.queue_files_in_order() == [
        engine.QUEUE / selected.name,
        duplicate_queue,
    ]
    assert engine.history_files_in_order() == []
    assert not engine.TIMELINE_INTENT.exists()


def test_promotion_migrates_a_legacy_sequence_collision_before_moving(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_promotion_deferred_duplicate")
    configure_runtime(engine, tmp_path)
    duplicate_history = engine.SPOKEN / "003-af_heart-say.txt"
    duplicate_queue = engine.QUEUE / duplicate_history.name
    selected = engine.SPOKEN / "002-af_heart-say.txt"
    duplicate_history.write_text("Legacy History copy", encoding="utf-8")
    duplicate_queue.write_text("Active copy", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    engine.save_queue_order([duplicate_queue])
    engine.save_history_order([duplicate_history, selected])
    target, _ = engine.promote_history_selection(selected)

    assert target == engine.QUEUE / selected.name
    assert not engine.TIMELINE_INTENT.exists()
    assert not list(engine.SPOKEN.glob("*.duplicate"))
    assert engine.queue_files_in_order()[0] == target
    assert any(
        path.read_text(encoding="utf-8") == "Legacy History copy"
        for path in [*engine.QUEUE.glob("*.txt"), *engine.SPOKEN.glob("*.txt")]
    )
    assert duplicate_queue.read_text(encoding="utf-8") == "Active copy"


def test_enqueue_waits_while_history_rows_are_moving(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_timeline_file_lock")
    configure_runtime(engine, tmp_path)
    lock = engine.InterprocessFileLock(engine.TIMELINE_LOCK)
    assert lock.acquire()
    queued: list[Path] = []
    writer = threading.Thread(
        target=lambda: queued.append(engine.enqueue_text("New", "af_heart"))
    )
    writer.start()
    threading.Event().wait(0.05)

    assert writer.is_alive()
    assert not list(engine.QUEUE.glob("*.txt"))

    lock.release()
    writer.join(1)
    assert not writer.is_alive()
    assert len(queued) == 1


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
    assert state.claims == {}
    release_save.set()
    promotion.join(1)
    worker.join(1)
    monkeypatch.setattr(engine, "save_queue_order", real_save)

    assert promotion_errors
    assert claimed == [None]
    assert state.claims == {}
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
    assert any(
        path.read_text(encoding="utf-8") == "Legacy replay"
        for path in engine.SPOKEN.glob("*.txt")
    )
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
    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )

    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None
    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "unconfirmed"
    assert result["snapshot"]["state"] == "stopped"


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

    original_id = speechicle_id(engine, archived)
    request_id = request_mutation(
        engine, "play", id=original_id, voice="af_heart"
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    result = committed_result(engine, request_id)
    variant = engine._find_chunk(engine.QUEUE, str(result["result_id"]))

    assert result["result_id"] == original_id
    assert variant is not None
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

    request_id = request_mutation(
        engine,
        "play",
        id=speechicle_id(engine, history[1]),
        voice="bm_fable",
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    result = committed_result(engine, request_id)
    after = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert [item["text"] for item in before["history"]] == [
        *[item["text"] for item in reversed(after["queue"])],
        after["current"]["text"],
        *[item["text"] for item in after["history"]],
    ]
    assert after["current"]["id"] == result["result_id"]
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

    request_id = request_mutation(
        engine,
        "play",
        id=speechicle_id(engine, history[-1]),
        voice="bm_fable",
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None

    result = rejected_result(engine, request_id)
    assert "could not replay" in str(result["error"])
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

    original_id = speechicle_id(engine, selected)
    request_id = request_mutation(
        engine, "play", id=original_id, voice="af_heart"
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    result = committed_result(engine, request_id)
    variant = engine._find_chunk(engine.QUEUE, str(result["result_id"]))

    assert result["result_id"] == original_id
    assert variant is not None
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
    engine._record_claim(state, current)

    original_id = speechicle_id(engine, current)
    request_id = request_mutation(
        engine, "play", id=original_id, voice="bm_fable"
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    result = committed_result(engine, request_id)
    variant = engine._find_chunk(engine.QUEUE, str(result["result_id"]))

    assert result["result_id"] == original_id
    assert variant is not None
    assert variant.name == "001-bm_fable-g600-say.txt"
    assert variant.read_text(encoding="utf-8") == "Current words"
    assert not current.exists()
    assert not (engine.SPOKEN / current.name).exists()
    assert engine.claim_next_queued_chunk(state) == variant


def test_play_mutation_rejects_a_missing_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_missing")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    request_id = request_mutation(
        engine, "play", id=f"sp_{'f' * 32}", voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None

    result = rejected_result(engine, request_id)
    assert "chunk not found" in str(result["error"])


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
    play_request = engine.BASE / f"MUTATION.1.{'a' * 24}.json"
    play_request.write_text(
        json.dumps(
            {
                "type": "play",
                "id": speechicle_id(engine, archived),
                "request_id": "a" * 24,
            }
        ),
        encoding="utf-8",
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
    assert final_status["current"] is None


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
    engine._record_claim(state, earlier)

    request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"

    assert state.playing == selected.name
    assert state.claims == {}
    assert engine.claim_next_queued_chunk(state) == selected


def test_selecting_a_prefetched_item_restarts_synthesis_at_piece_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_prefetched_piece")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    monkeypatch.setattr(engine, "SPLIT_CHARS", 16)
    current = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    selected.write_text("First sentence. Second sentence.", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    engine._record_claim(state, current)
    entered_second_piece = threading.Event()
    release_second_piece = threading.Event()
    synthesis_calls: list[str] = []

    class BlockingKokoro:
        @staticmethod
        def create(text: str, **_kwargs):
            synthesis_calls.append(text)
            if synthesis_calls == ["First sentence.", "Second sentence."]:
                entered_second_piece.set()
                assert release_second_piece.wait(1)
            return np.ones(2, dtype=np.float32), 2

    buffered: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=engine.synth_worker,
        args=(BlockingKokoro(), buffered, state),
    )
    worker.start()
    try:
        assert entered_second_piece.wait(1)
        request_mutation(
            engine, "play", id=speechicle_id(engine, selected), voice=None
        )
        assert engine.process_mutation_requests(buffered, state) == "select"
        assert buffered.empty()
        release_second_piece.set()
        restarted = buffered.get(timeout=1)
    finally:
        release_second_piece.set()
        state.stop.set()
        worker.join(1)

    assert not worker.is_alive()
    assert restarted[0] == selected
    assert restarted[5] == 1
    assert restarted[7] == "First sentence. Second sentence."


def test_selection_interrupts_an_inter_chunk_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_gap")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()

    request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )

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
    engine._record_claim(state, held)
    request_id = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, selected),
        before_id=speechicle_id(engine, held),
    )

    assert engine.gap_wait(1.0, queue.Queue(), state) == "queue_changed"
    committed_result(engine, request_id)

    assert held.is_file()
    assert state.claims == {}
    assert engine.claim_next_queued_chunk(state) == selected


def test_queue_mutation_invalidates_the_piece_held_before_playback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_held_piece_generation")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-bm_fable-say.txt"
    moved = engine.QUEUE / "003-af_heart-say.txt"
    for path in (current, waiting, moved):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    first_claim = engine.claim_next_queued_chunk_with_generation(state)
    assert first_claim is not None
    held_path, old_generation = first_claim
    request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, moved),
        before_id=speechicle_id(engine, waiting),
    )

    assert (
        engine.process_mutation_requests(queue.Queue(), state, held_path.name)
        == "queue_changed"
    )
    replacement_claim = engine.claim_next_queued_chunk_with_generation(state)

    assert replacement_claim is not None
    replacement_path, new_generation = replacement_claim
    assert replacement_path == held_path
    assert new_generation != old_generation
    assert engine.buffered_piece_is_stale(state, held_path.name, old_generation)
    assert not engine.buffered_piece_is_stale(state, held_path.name, new_generation)


def test_stale_worker_release_preserves_a_replacement_current_claim(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_stale_worker_claim_release")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    state.selection_name = current.name
    _, old_generation = engine._record_claim(state, current)
    engine.invalidate_claim(state, current.name)
    _, replacement_generation = engine._record_claim(state, current)

    engine.release_preplay_chunk(state, current.name, old_generation)

    assert state.claims == {current.name: replacement_generation}
    assert state.playing == current.name
    assert state.selection_name == current.name


def test_queue_mutation_keeps_an_active_playback_claim_without_buffered_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_active_piece_generation")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-bm_fable-say.txt"
    moved = engine.QUEUE / "003-af_heart-say.txt"
    for path in (current, waiting, moved):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    claim = engine.claim_next_queued_chunk_with_generation(state)
    assert claim is not None
    current_path, generation = claim
    request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, moved),
        before_id=speechicle_id(engine, waiting),
    )

    assert engine.process_mutation_requests(queue.Queue(), state) == "queue_changed"

    assert set(state.claims) == {current.name}
    assert not engine.buffered_piece_is_stale(state, current_path.name, generation)


def test_queue_mutation_invalidates_the_piece_held_during_a_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_gap_generation")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-bm_fable-say.txt"
    moved = engine.QUEUE / "003-af_heart-say.txt"
    for path in (current, waiting, moved):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    first_claim = engine.claim_next_queued_chunk_with_generation(state)
    assert first_claim is not None
    held_path, old_generation = first_claim
    request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, moved),
        before_id=speechicle_id(engine, waiting),
    )

    assert engine.gap_wait(1.0, queue.Queue(), state) == "queue_changed"
    replacement_claim = engine.claim_next_queued_chunk_with_generation(state)

    assert replacement_claim is not None
    replacement_path, new_generation = replacement_claim
    assert replacement_path == held_path
    assert new_generation != old_generation
    assert engine.buffered_piece_is_stale(state, held_path.name, old_generation)
    assert not engine.buffered_piece_is_stale(state, held_path.name, new_generation)


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
    engine._record_claim(state, current)
    monkeypatch.setattr(engine, "archive", lambda path: False)
    monkeypatch.setattr(engine, "archive_failed", lambda path: False)

    assert engine.finish_chunk_playback(current, outcome, True, state)

    assert state.playing is None
    assert set(state.claims) == {current.name}
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
    engine._record_claim(state, waiting)
    monkeypatch.setattr(engine, "_archive_many", lambda _paths: False)

    engine.do_clear(queue.Queue(), state)

    assert waiting.exists()
    assert state.claims == {}
    assert state.stop.is_set()


def test_partial_clear_failure_rolls_back_the_visible_timeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_clear_rollback")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    first_waiting = engine.QUEUE / "002-af_heart-say.txt"
    locked_waiting = engine.QUEUE / "003-af_heart-say.txt"
    for path in (current, first_waiting, locked_waiting):
        path.write_text(path.stem, encoding="utf-8")
    ordered = [current, first_waiting, locked_waiting]
    engine.save_queue_order(ordered)
    state = engine.State()
    state.playing = current.name
    real_replace = engine.os.replace

    def fail_second(source, destination) -> None:
        if Path(source) == locked_waiting and Path(destination).parent == engine.SPOKEN:
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_second)

    engine.do_clear(queue.Queue(), state)

    assert engine.queue_files_in_order() == ordered
    assert not list(engine.SPOKEN.glob("*.txt"))
    assert state.stop.is_set()


def test_clear_archives_each_buffered_chunk_only_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_clear_unique_paths")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    engine._record_claim(state, waiting)
    buffer: queue.Queue = queue.Queue()
    buffer.put((waiting,))
    buffer.put((waiting,))

    engine.do_clear(buffer, state)

    assert not waiting.exists()
    assert (engine.SPOKEN / waiting.name).exists()
    assert not state.stop.is_set()
    assert state.claims == {}
    assert (engine.SPOKEN / waiting.name).exists()


def test_clear_preserves_the_visible_order_of_waiting_speech(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_stable_order")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    first_waiting = engine.QUEUE / "003-af_heart-say.txt"
    second_waiting = engine.QUEUE / "002-af_heart-say.txt"
    for path in (current, first_waiting, second_waiting):
        path.write_text(path.stem, encoding="utf-8")
    engine.save_queue_order([current, first_waiting, second_waiting])
    state = engine.State()
    state.playing = current.name

    engine.do_clear(queue.Queue(), state)

    assert not current.exists()
    assert engine.history_files_in_order() == [
        engine.SPOKEN / second_waiting.name,
        engine.SPOKEN / first_waiting.name,
        engine.SPOKEN / current.name,
    ]


def test_clear_does_not_manufacture_a_claim_for_a_preplay_current(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_preplay_claim")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    state.playing = current.name

    engine.do_clear(queue.Queue(), state)

    assert state.claims == {}
    assert state.playing is None
    assert engine.claim_next_queued_chunk(state) is None
    assert not current.exists()
    assert not waiting.exists()


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
    assert set(state.claims) == {waiting.name}
    assert state.stop.is_set()


def test_mid_item_synthesis_failure_emits_one_terminal_piece_and_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_mid_item_synth_failure")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("One. Two. Three.", encoding="utf-8")
    pieces = [
        SimpleNamespace(text=text, start=index * 5, end=index * 5 + len(text))
        for index, text in enumerate(("One.", "Two.", "Three."))
    ]
    monkeypatch.setattr(engine, "split_text_pieces", lambda *_args: pieces)
    calls: list[str] = []

    class Kokoro:
        @staticmethod
        def create(text: str, **_kwargs):
            calls.append(text)
            if text == "Two.":
                raise RuntimeError("piece failed")
            if text == "Three.":
                raise AssertionError("synthesis continued past a terminal failure")
            return np.ones(8, dtype=np.float32), 8

    state = engine.State()
    buffer: queue.Queue = queue.Queue()
    worker = threading.Thread(
        target=engine.synth_worker,
        args=(Kokoro(), buffer, state),
    )
    worker.start()
    first = buffer.get(timeout=1)
    terminal = buffer.get(timeout=1)
    threading.Event().wait(0.05)
    state.stop.set()
    worker.join(1)

    assert not worker.is_alive()
    assert calls == ["One.", "Two."]
    assert len(first[1]) == 8
    assert first[4] is False
    assert len(terminal[1]) == 0
    assert terminal[4] is True
    assert terminal[5] == 2
    with pytest.raises(queue.Empty):
        buffer.get_nowait()


def test_invalidated_synthesis_failure_cannot_stop_the_selected_boundary(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_stale_synthesis_failure")
    configure_runtime(engine, tmp_path)
    old = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-af_heart-say.txt"
    old.write_text("Old", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    entered_synthesis = threading.Event()
    release_synthesis = threading.Event()
    selected_synthesis = threading.Event()
    finish_test = threading.Event()
    calls = 0
    state = engine.State()
    state.playing = old.name

    class FailingKokoro:
        @staticmethod
        def create(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered_synthesis.set()
                assert release_synthesis.wait(1)
                raise RuntimeError("stale failure")
            selected_synthesis.set()
            assert finish_test.wait(1)
            return np.ones(8, dtype=np.float32), 8

    worker = threading.Thread(
        target=engine.synth_worker,
        args=(FailingKokoro(), queue.Queue(), state),
    )
    worker.start()
    assert entered_synthesis.wait(1)
    with state.lock:
        state.claims.clear()
        state.playing = selected.name
        state.selection_name = selected.name
    assert engine.archive(old)
    release_synthesis.set()
    assert selected_synthesis.wait(1)
    assert not state.stop.is_set()
    assert not (engine.FAILED / old.name).exists()
    state.stop.set()
    finish_test.set()
    worker.join(1)

    assert not worker.is_alive()


def test_clear_wins_a_synthesis_failure_after_the_first_claim_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_clear_synth_failure_race")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    entered_error_log = threading.Event()
    release_error_log = threading.Event()
    original_log = engine.log

    class FailingKokoro:
        @staticmethod
        def create(*_args, **_kwargs):
            raise RuntimeError("synthesis failed")

    def block_after_claim_check(message: str, **kwargs) -> None:
        if message.startswith("synth error"):
            entered_error_log.set()
            assert release_error_log.wait(1)
        original_log(message, **kwargs)

    monkeypatch.setattr(engine, "log", block_after_claim_check)
    worker = threading.Thread(
        target=engine.synth_worker,
        args=(FailingKokoro(), queue.Queue(), state),
    )
    worker.start()
    assert entered_error_log.wait(1)

    assert engine.do_clear(queue.Queue(), state)
    release_error_log.set()
    threading.Event().wait(0.05)

    assert not state.stop.is_set()
    assert not (engine.FAILED / current.name).exists()
    assert (engine.SPOKEN / current.name).exists()
    state.stop.set()
    worker.join(1)
    assert not worker.is_alive()


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
    assert state.claims == {}


def test_transient_current_read_failure_remains_claimable_during_stop(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_current_read_failure")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    state.playing = waiting.name
    engine._record_claim(state, waiting)
    with state.lock:
        state.claims.pop(waiting.name, None)
    state.saw_stop = True

    assert waiting.exists()
    assert engine.claim_next_queued_chunk(state) == waiting


def test_persistent_current_read_failure_cannot_hang_graceful_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_current_read_failure_stop")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    state.playing = waiting.name
    state.saw_stop = True
    original_read_text = Path.read_text

    def locked_current(path: Path, *args, **kwargs) -> str:
        if path == waiting:
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked_current)
    worker = threading.Thread(
        target=engine.synth_worker,
        args=(SimpleNamespace(), queue.Queue(), state),
    )
    worker.start()
    worker.join(1)

    assert not worker.is_alive()
    assert state.stop.is_set()
    assert state.playing is None
    assert waiting.exists()


def test_stop_during_a_gap_keeps_the_next_chunk_queued(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_stop_gap")
    configure_runtime(engine, tmp_path)
    next_chunk = engine.QUEUE / "002-bm_fable-say.txt"
    next_chunk.write_text("Next", encoding="utf-8")
    state = engine.State()
    engine._record_claim(state, next_chunk)
    engine.STOP.touch()

    assert engine.gap_wait(1.0, queue.Queue(), state) == "stop"
    assert next_chunk.is_file()


def test_fatal_engine_stop_ends_a_gap_before_the_next_chunk(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_fatal_gap")
    configure_runtime(engine, tmp_path)
    state = engine.State()
    state.stop.set()

    assert engine.gap_wait(1.0, queue.Queue(), state) == "fatal"


@pytest.mark.parametrize(
    ("clear_time", "stop_time", "expected"),
    [(1_000_000_000, 2_000_000_000, "stop"), (2_000_000_000, 1_000_000_000, "clear")],
)
def test_clear_and_stop_follow_publication_order_during_a_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clear_time: int,
    stop_time: int,
    expected: str,
) -> None:
    engine = load_engine(f"super_speech_engine_clear_stop_order_{expected}")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.SIGNAL_TICK = 0.001
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    request_id = request_mutation(engine, "clear")
    request = next(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))
    engine.STOP.touch()
    os.utime(request, ns=(clear_time, clear_time))
    os.utime(engine.STOP, ns=(stop_time, stop_time))

    assert engine.gap_wait(0.01, queue.Queue(), state) == expected
    if expected == "stop":
        assert current.exists()
        assert not (engine.SPOKEN / current.name).exists()
    else:
        assert not current.exists()
        assert (engine.SPOKEN / current.name).exists()
        assert state.playing is None


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


@pytest.mark.parametrize("play_is_newer", [False, True])
def test_play_and_stop_follow_publication_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    play_is_newer: bool,
) -> None:
    engine = load_engine(f"super_speech_engine_play_stop_order_{play_is_newer}")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    state.saw_stop = True
    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, current), voice=None
    )
    request = next(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))
    engine.STOP.touch()
    play_time, stop_time = (
        (2_000_000_000, 1_000_000_000)
        if play_is_newer
        else (1_000_000_000, 2_000_000_000)
    )
    os.utime(request, ns=(play_time, play_time))
    os.utime(engine.STOP, ns=(stop_time, stop_time))

    engine.process_mutation_requests(queue.Queue(), state)

    if play_is_newer:
        assert committed_result(engine, request_id)["result_id"] == speechicle_id(
            engine, current
        )
        assert not engine.STOP.exists()
        assert not state.saw_stop
    else:
        result = rejected_result(engine, request_id)
        assert "engine stopped" in str(result["error"])
        assert engine.STOP.exists()
        assert state.saw_stop


def test_a_newer_stop_is_not_canceled_by_an_older_new_work_notice(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_newer_stop_wins")
    configure_runtime(engine, tmp_path)
    engine.SIGNAL_TICK = 0.001
    state = engine.State()
    engine.CONTINUE.touch()
    engine.STOP.touch()
    os.utime(engine.CONTINUE, ns=(1_000_000_000, 1_000_000_000))
    os.utime(engine.STOP, ns=(2_000_000_000, 2_000_000_000))

    assert engine.gap_wait(0.01, queue.Queue(), state) == "stop"
    assert not engine.CONTINUE.exists()


def test_fatal_selection_failure_dominates_skip_during_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_fatal_gap_selection")
    configure_runtime(engine, tmp_path)
    engine.SIGNAL_TICK = 0.001
    state = engine.State()
    engine.SKIP.touch()

    def fail_selection(_buf, failed_state, _held_chunk_name) -> None:
        failed_state.stop.set()

    monkeypatch.setattr(engine, "process_mutation_requests", fail_selection)

    assert engine.gap_wait(0.01, queue.Queue(), state) == "fatal"
    assert engine.SKIP.exists()


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
        engine, "public_id_for_path", lambda _path: f"sp_{'1' * 32}"
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
    queued = engine._find_chunk(engine.QUEUE, captured.out.strip())
    assert queued is not None
    assert queued.read_text(encoding="utf-8") == "New work"


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


def test_clear_during_a_gap_does_not_play_the_archived_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_clear_gap")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    next_chunk = engine.QUEUE / "002-bm_fable-say.txt"
    next_chunk.write_text("Next", encoding="utf-8")
    state = engine.State()
    engine._record_claim(state, next_chunk)
    request_mutation(engine, "clear")

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
    engine._record_claim(state, current)

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

    with pytest.raises(queue.Empty):
        buffer.get_nowait()
    assert not worker.is_alive()
    assert state.claims == {}
    assert (engine.SPOKEN / current.name).read_text(encoding="utf-8") == "Current"
    assert (engine.SPOKEN / queued.name).read_text(encoding="utf-8") == "Queued"


def test_startup_cleanup_removes_protocol_11_request_artifacts(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_remove_v11_requests")
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
    assert not request.exists()
    assert not claim.exists()
    assert not temporary.exists()
    assert not acknowledgement.exists()
    assert not queue_request.exists()
    assert not queue_acknowledgement.exists()


def test_startup_rejects_an_abandoned_claim_without_leaving_its_caller_waiting(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_recover_claim")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    claim = engine.BASE / f"MUTATION.1.{request_id}.claim"
    claim.write_text(
        json.dumps(
            {
                "type": "play",
                "id": f"sp_{'1' * 32}",
                "request_id": request_id,
            }
        ),
        encoding="utf-8",
    )

    state = engine.State()
    engine.publish_status("idle", state, force=True)
    engine.settle_stale_mutation_claims(state)

    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "unconfirmed"
    assert "engine restarted" in str(result["error"])
    assert not claim.exists()


def test_failed_result_keeps_the_applied_claim_without_repeating_the_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_applied_claim")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    history = engine.SPOKEN / "001-af_heart-say.txt"
    history.write_text("Delete once", encoding="utf-8")
    request_id = request_mutation(
        engine, "delete", id=speechicle_id(engine, history)
    )
    monkeypatch.setattr(engine, "publish_mutation_result", lambda *_args, **_kwargs: False)

    state = engine.State()
    assert engine.process_mutation_requests(queue.Queue(), state) is None
    claims = list(engine.BASE.glob(f"MUTATION.*.{request_id}.claim"))

    assert len(claims) == 1
    assert not history.exists()
    assert engine.process_mutation_requests(queue.Queue(), state) is None
    with pytest.raises(RuntimeError, match="result was unconfirmed"):
        engine.wait_for_mutation_result(request_id, timeout=0.01)


def test_claimed_request_returns_unconfirmed_when_successor_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_claim_restart_failure")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    claim = engine.BASE / f"MUTATION.1.{request_id}.claim"
    claim.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(engine, "engine_is_running", lambda: False)

    def fail_start() -> None:
        raise RuntimeError("forced startup failure")

    monkeypatch.setattr(engine, "start_engine", fail_start)

    with pytest.raises(RuntimeError, match="result was unconfirmed"):
        engine.wait_for_mutation_result(request_id, timeout=0.01)

    assert claim.exists()


def test_mutation_result_pruning_keeps_active_waiters(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_result_pruning")
    configure_runtime(engine, tmp_path)
    stale = engine.BASE / f"MUTATION_RESULT.{'a' * 24}.json"
    active = engine.BASE / f"MUTATION_RESULT.{'b' * 24}.json"
    stale.write_text("{}", encoding="utf-8")
    active.write_text("{}", encoding="utf-8")
    os.utime(stale, (0, 0))

    engine.prune_mutation_results()

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
        engine,
        "run_engine_loop",
        lambda *_args: observed.append(engine.STATUS.exists()),
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

    engine.apply_queue_command(
        queue.Queue(),
        engine.State(),
        "move_history",
        speechicle_id(engine, third),
        None,
    )

    assert engine.history_files_in_order() == [second, third, first]
    assert [item["id"] for item in engine.history_snapshot()[1]] == [
        speechicle_id(engine, second),
        speechicle_id(engine, third),
    ]


def test_new_history_item_stays_newest_after_manual_reordering(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_newest")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-af_heart-say.txt"
    second = engine.SPOKEN / "002-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    engine.save_history_order([first, second])
    newest = engine.enqueue_text("Newest", "af_heart")

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
    second_id = speechicle_id(engine, second)
    first_id = speechicle_id(engine, first)
    reorder = threading.Thread(
        name="history-reorder",
        target=engine.apply_queue_command,
        args=(queue.Queue(), engine.State(), "move_history", second_id, first_id),
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
    real_write = engine._write_saved_order
    real_history_files = engine.history_files_in_order

    def gated_write(path: Path, ids: list[str]) -> None:
        if path == engine.HISTORY_ORDER and threading.current_thread().name == "archive":
            archive_holds_order.set()
            snapshot_reading.wait(0.1)
        real_write(path, ids)

    def observed_history_files() -> list[Path]:
        if threading.current_thread().name == "snapshot":
            snapshot_reading.set()
        return real_history_files()

    monkeypatch.setattr(engine, "_write_saved_order", gated_write)
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

    assert [item["text"] for item in history] == [
        "10224-af_heart-say.txt",
        "10223-af_heart-say.txt",
        "8552a-af_bella-say.txt",
    ]
    assert all(engine.is_public_id(item["id"]) for item in history)


def test_history_snapshot_refreshes_only_after_an_archive_move(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_cache")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")

    first_count, first_items = engine.history_snapshot()
    cached_count, cached_items = engine.history_snapshot()

    assert first_count == cached_count == 1
    assert cached_items is first_items

    second = engine.enqueue_text("Second", "bm_fable")
    assert engine.archive(second)
    refreshed_count, refreshed_items = engine.history_snapshot()

    assert refreshed_count == 2
    assert refreshed_items is not first_items
    assert [item["text"] for item in refreshed_items] == ["Second", "First"]


def test_history_snapshot_keeps_rows_during_a_transient_text_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_text_lock")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    assert engine.history_snapshot()[1][0]["text"] == "First"
    queued_second = engine.enqueue_text("Second", "af_heart")
    assert engine.archive(queued_second)
    second = engine.SPOKEN / queued_second.name
    original_read_text = Path.read_text

    def locked_text(path: Path, *args, **kwargs) -> str:
        if path in {first, second}:
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked_text)
    count, items = engine.history_snapshot()

    assert count == len(items) == 2
    assert [item["id"] for item in items] == [
        speechicle_id(engine, second),
        speechicle_id(engine, first),
    ]
    assert items[0]["text"] == ""
    assert items[1]["text"] == "First"


def test_startup_completes_an_interrupted_clear_batch(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_batch_recovery")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-af_heart-say.txt"
    earlier = engine.SPOKEN / "003-af_heart-say.txt"
    for path in (current, waiting, earlier):
        path.write_text(path.stem, encoding="utf-8")
    engine.save_queue_order([current, waiting])
    engine.save_history_order([earlier])
    engine._write_timeline_intent(
        {
            "version": 1,
            "operation": "archive_batch",
            "order_version": 2,
            "moves": [
                {"source": current.name, "target": current.name},
                {"source": waiting.name, "target": waiting.name},
            ],
            "previous_queue_ids": [
                speechicle_id(engine, current),
                speechicle_id(engine, waiting),
            ],
            "previous_history_ids": [speechicle_id(engine, earlier)],
            "queue_ids": [],
            "history_ids": [
                speechicle_id(engine, waiting),
                speechicle_id(engine, current),
                speechicle_id(engine, earlier),
            ],
        }
    )
    os.replace(current, engine.SPOKEN / current.name)

    engine.repair_interrupted_timeline_transition()

    assert engine.queue_files_in_order() == []
    assert engine.history_files_in_order() == [
        engine.SPOKEN / waiting.name,
        engine.SPOKEN / current.name,
        earlier,
    ]
    assert not engine.TIMELINE_INTENT.exists()


def test_archive_succeeds_when_committed_intent_cleanup_is_deferred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_deferred_cleanup")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    engine.ensure_identity_catalog()
    waiting = engine.enqueue_text("Waiting", "af_heart")
    real_unlink = Path.unlink

    def locked_intent(path: Path, *args, **kwargs) -> None:
        if path == engine.TIMELINE_INTENT:
            raise PermissionError("locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_intent)
    assert engine.archive(current)
    assert not current.exists()
    assert (engine.SPOKEN / current.name).exists()
    assert engine.TIMELINE_INTENT.exists()
    pending_intent = engine.TIMELINE_INTENT.read_text(encoding="utf-8")
    with pytest.raises(PermissionError, match="locked"):
        engine.archive(waiting)
    assert waiting.exists()
    assert engine.TIMELINE_INTENT.read_text(encoding="utf-8") == pending_intent

    monkeypatch.setattr(Path, "unlink", real_unlink)
    engine.repair_interrupted_timeline_transition()
    assert not engine.TIMELINE_INTENT.exists()
    assert engine.history_files_in_order() == [engine.SPOKEN / current.name]


def test_archive_rollback_does_not_resurrect_an_already_archived_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_no_resurrection")
    configure_runtime(engine, tmp_path)
    archived = engine.SPOKEN / "001-af_heart-say.txt"
    blocked = engine.QUEUE / "002-af_heart-say.txt"
    archived.write_text("Earlier", encoding="utf-8")
    blocked.write_text("Blocked", encoding="utf-8")
    real_replace = engine.os.replace

    def fail_blocked(source, destination) -> None:
        if Path(source) == blocked and Path(destination).parent == engine.SPOKEN:
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_blocked)

    assert not engine._archive_many([engine.QUEUE / archived.name, blocked])
    assert not (engine.QUEUE / archived.name).exists()
    assert archived.exists()
    assert blocked.exists()


def test_archive_rollback_restores_a_preexisting_history_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_duplicate_rollback")
    configure_runtime(engine, tmp_path)
    duplicate_queue = engine.QUEUE / "001-af_heart-say.txt"
    duplicate_history = engine.SPOKEN / duplicate_queue.name
    blocked = engine.QUEUE / "002-af_heart-say.txt"
    duplicate_queue.write_text("Active copy", encoding="utf-8")
    duplicate_history.write_text("History copy", encoding="utf-8")
    blocked.write_text("Blocked", encoding="utf-8")
    engine.save_queue_order([duplicate_queue, blocked])
    engine.save_history_order([duplicate_history])
    migrated_history = next(
        path
        for path in engine.SPOKEN.glob("*.txt")
        if path.read_text(encoding="utf-8") == "History copy"
    )
    real_replace = engine.os.replace

    def fail_blocked(source, destination) -> None:
        if Path(source) == blocked and Path(destination).parent == engine.SPOKEN:
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_blocked)

    assert not engine._archive_many([duplicate_queue, blocked])
    assert duplicate_queue.read_text(encoding="utf-8") == "Active copy"
    assert migrated_history.read_text(encoding="utf-8") == "History copy"
    assert blocked.read_text(encoding="utf-8") == "Blocked"
    assert engine.queue_files_in_order() == [duplicate_queue, blocked]
    assert engine.history_files_in_order() == [migrated_history]
    assert not engine.TIMELINE_INTENT.exists()


def test_unconfirmed_archive_rollback_retains_its_recovery_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_unconfirmed_recovery")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    engine.save_queue_order([current, waiting])
    real_replace = engine.os.replace

    def fail_forward_and_rollback(source, destination) -> None:
        source = Path(source)
        destination = Path(destination)
        if source == waiting or (
            source == engine.SPOKEN / current.name and destination == current
        ):
            raise PermissionError("locked")
        real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_forward_and_rollback)

    with pytest.raises(engine.MutationOutcomeUnconfirmed):
        engine._archive_many([current, waiting])
    assert engine.TIMELINE_INTENT.exists()

    monkeypatch.setattr(engine.os, "replace", real_replace)
    engine.repair_interrupted_timeline_transition()

    assert engine.queue_files_in_order() == []
    assert engine.history_files_in_order() == [
        engine.SPOKEN / waiting.name,
        engine.SPOKEN / current.name,
    ]
    assert not engine.TIMELINE_INTENT.exists()


def test_clear_archives_a_paused_current_and_publishes_idle(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_paused_current")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    current.write_text("Paused current", encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    state.current_text = "Paused current"
    state.current_voice = "af_heart"
    state.current_piece_count = 1
    engine.PAUSE.touch()

    assert engine.do_clear(queue.Queue(), state)
    engine.publish_status("idle", state, force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "idle"
    assert status["current"] is None
    assert status["queue"] == []
    assert [item["id"] for item in status["history"]] == [
        speechicle_id(engine, current)
    ]


def test_identity_migration_recovers_with_the_ids_saved_before_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interrupted = load_engine("super_speech_engine_identity_interrupted")
    configure_runtime(interrupted, tmp_path)
    queued = interrupted.QUEUE / "004-af_heart-say.txt"
    collision = interrupted.SPOKEN / "004-bm_fable-say.txt"
    malformed = interrupted.SPOKEN / "4oops-af_bella-say.txt"
    queued.write_text("Queued", encoding="utf-8")
    collision.write_text("Collision", encoding="utf-8")
    malformed.write_text("Malformed", encoding="utf-8")
    interrupted._write_order_payload(interrupted.QUEUE_ORDER, [queued.stem], 1)
    interrupted._write_order_payload(
        interrupted.HISTORY_ORDER, [malformed.stem, collision.stem], 1
    )

    def stop_after_journal(_intent: dict[str, object]) -> None:
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(
        interrupted, "_apply_identity_migration_intent", stop_after_journal
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        interrupted.ensure_identity_catalog()
    saved_intent = json.loads(
        interrupted.TIMELINE_INTENT.read_text(encoding="utf-8")
    )

    recovered = load_engine("super_speech_engine_identity_recovered")
    configure_runtime(recovered, tmp_path)
    recovered.repair_interrupted_timeline_transition()

    catalog = json.loads(recovered.IDENTITY_INDEX.read_text(encoding="utf-8"))
    assert catalog == saved_intent["catalog"]
    assert json.loads(recovered.QUEUE_ORDER.read_text(encoding="utf-8")) == {
        "version": 2,
        "ids": saved_intent["queue_ids"],
    }
    assert json.loads(recovered.HISTORY_ORDER.read_text(encoding="utf-8")) == {
        "version": 2,
        "ids": saved_intent["history_ids"],
    }
    files = [
        *recovered.QUEUE.glob("*.txt"),
        *recovered.SPOKEN.glob("*.txt"),
        *recovered.FAILED.glob("*.txt"),
    ]
    sequences = [recovered.strict_sequence(path.name) for path in files]
    assert None not in sequences
    assert len(set(sequences)) == len(sequences)
    assert not recovered.TIMELINE_INTENT.exists()


def test_deleted_identity_and_sequence_are_never_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_identity_delete")
    configure_runtime(engine, tmp_path)
    first = engine.enqueue_text("First", "af_heart")
    first_id = speechicle_id(engine, first)
    first_sequence = engine.strict_sequence(first.name)
    assert engine.archive(first)
    engine.apply_queue_command(
        queue.Queue(), engine.State(), "delete", first_id, None
    )

    second = engine.enqueue_text("Second", "af_heart")
    second_id = speechicle_id(engine, second)

    assert engine.strict_sequence(second.name) > first_sequence
    assert second_id != first_id
    assert engine._find_chunk(engine.QUEUE, first_id) is None
    assert engine._find_chunk(engine.SPOKEN, first_id) is None

    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = request_mutation(engine, "play", id=first_id, voice=None)
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None
    result = rejected_result(engine, request_id)
    assert "chunk not found" in str(result["error"])


def test_voice_and_history_lifecycle_preserve_one_public_identity(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_identity_lifecycle")
    configure_runtime(engine, tmp_path)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    original = engine.enqueue_text("Same words", "af_heart", 250)
    public_id = speechicle_id(engine, original)

    changed = engine._replace_queue_voice(original, "bm_fable")
    assert speechicle_id(engine, changed) == public_id
    assert engine.archive(changed)
    archived = engine._find_chunk(engine.SPOKEN, public_id)
    assert archived is not None

    replayed, _ = engine.promote_history_selection(archived, "af_heart")

    assert speechicle_id(engine, replayed) == public_id
    assert replayed.name.endswith("-af_heart-g250-say.txt")


def test_public_status_contains_no_storage_filenames(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_public_status_shape")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-af_heart-say.txt"
    archived = engine.SPOKEN / "002-bm_fable-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    archived.write_text("History", encoding="utf-8")

    engine.publish_status("idle", engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["version"] == engine.STATUS_VERSION
    assert "filename" not in json.dumps(status)
    visible = [
        *([status["current"]] if status["current"] else []),
        *status["queue"],
        *status["history"],
    ]
    assert visible
    assert all(engine.is_public_id(item["id"]) for item in visible)
    assert not engine.PAUSE.exists()


@pytest.mark.parametrize(
    ("payload", "variant_name"),
    [
        (
            {"request_id": "a" * 24, "type": "play", "id": f"sp_{'1' * 32}"},
            "PlayMutation",
        ),
        (
            {
                "request_id": "b" * 24,
                "type": "move",
                "section": "waiting",
                "id": f"sp_{'1' * 32}",
                "before_id": None,
            },
            "MoveMutation",
        ),
        (
            {"request_id": "c" * 24, "type": "archive", "id": f"sp_{'1' * 32}"},
            "ArchiveMutation",
        ),
        (
            {"request_id": "d" * 24, "type": "delete", "id": f"sp_{'1' * 32}"},
            "DeleteMutation",
        ),
        ({"request_id": "e" * 24, "type": "clear"}, "ClearMutation"),
    ],
)
def test_mutation_envelope_accepts_each_variant(
    payload: dict[str, object], variant_name: str
) -> None:
    engine = load_engine(f"super_speech_engine_envelope_{payload['type']}")

    parsed = engine.parse_durable_mutation(payload)

    assert type(parsed).__name__ == variant_name
    assert parsed.to_payload() == payload
    with pytest.raises(FrozenInstanceError):
        parsed.request_id = "f" * 24


def test_mutation_variants_only_expose_their_valid_fields() -> None:
    engine = load_engine("super_speech_engine_mutation_variant_fields")
    public_id = f"sp_{'1' * 32}"

    play = engine.parse_durable_mutation(
        {"request_id": "a" * 24, "type": "play", "id": public_id}
    )
    clear = engine.parse_durable_mutation({"request_id": "b" * 24, "type": "clear"})

    assert play.id == public_id
    assert not hasattr(play, "section")
    assert not hasattr(clear, "id")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"request_id": "short", "type": "clear"},
        {"request_id": "a" * 24, "type": "unknown"},
        {"request_id": "a" * 24, "type": "play", "id": "../history"},
        {
            "request_id": "a" * 24,
            "type": "move",
            "section": "current",
            "id": f"sp_{'1' * 32}",
            "before_id": None,
        },
        {"request_id": "a" * 24, "type": "clear", "id": f"sp_{'1' * 32}"},
    ],
)
def test_mutation_envelope_rejects_invalid_shapes(payload: object) -> None:
    engine = load_engine("super_speech_engine_invalid_envelope")

    with pytest.raises(ValueError):
        engine.parse_durable_mutation(payload)


def test_mutations_commit_and_publish_results_in_one_fifo_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_mutation_fifo")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-bm_fable-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    first_id = speechicle_id(engine, first)
    second_id = speechicle_id(engine, second)
    move_request = request_mutation(
        engine,
        "move",
        section="waiting",
        id=second_id,
        before_id=first_id,
    )
    play_request = request_mutation(
        engine, "play", id=second_id, voice=None
    )
    published: list[str] = []
    real_publish = engine.publish_mutation_result

    def publish(request_id: str, *args, **kwargs) -> bool:
        published.append(request_id)
        return real_publish(request_id, *args, **kwargs)

    monkeypatch.setattr(engine, "publish_mutation_result", publish)

    assert engine.process_mutation_requests(queue.Queue(), engine.State()) == "queue_changed"

    assert published == [move_request, play_request]
    assert committed_result(engine, move_request)["snapshot"]["current"]["id"] == second_id
    play_result = committed_result(engine, play_request)
    assert play_result["result_id"] == second_id
    assert play_result["snapshot"]["current"]["id"] == second_id


def test_later_waiting_move_does_not_hide_an_earlier_playback_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_mutation_effect_priority")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "002-bm_fable-say.txt"
    waiting = engine.QUEUE / "003-af_heart-say.txt"
    for path in (current, selected, waiting):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    state.playing = current.name
    request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, waiting),
        before_id=None,
    )

    assert engine.process_mutation_requests(queue.Queue(), state) == "select"


def test_every_mutation_outcome_contains_an_authoritative_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_mutation_outcomes")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    state.playing = current.name

    committed_request = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, waiting),
        before_id=None,
    )
    rejected_request = request_mutation(
        engine, "delete", id=speechicle_id(engine, waiting)
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "queue_changed"

    committed = committed_result(engine, committed_request)
    rejected = rejected_result(engine, rejected_request)
    assert committed["snapshot"]["version"] == engine.STATUS_VERSION
    assert rejected["snapshot"]["version"] == engine.STATUS_VERSION

    unconfirmed_request = request_mutation(
        engine, "archive", id=speechicle_id(engine, waiting)
    )
    monkeypatch.setattr(
        engine,
        "archive",
        lambda _path: (_ for _ in ()).throw(
            engine.MutationOutcomeUnconfirmed("rollback failed")
        ),
    )
    engine.process_mutation_requests(queue.Queue(), state)
    unconfirmed = engine.wait_for_mutation_result(unconfirmed_request, timeout=0.1)

    assert unconfirmed["outcome"] == "unconfirmed"
    assert unconfirmed["snapshot"]["version"] == engine.STATUS_VERSION
    assert unconfirmed["snapshot"]["state"] == "stopped"


def test_timeline_revision_changes_only_with_the_timeline_and_survives_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_timeline_revision")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.QUEUE / "002-af_heart-say.txt"
    third = engine.QUEUE / "003-af_heart-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    initial = engine.publish_status("playing", state, force=True)
    assert initial is not None
    assert initial["timeline_revision"] == 0

    state.current_piece = 2
    engine.PAUSE.touch()
    progress = engine.publish_status("paused", state, force=True)
    assert progress is not None
    assert progress["timeline_revision"] == 0

    move_request = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, third),
        before_id=speechicle_id(engine, second),
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "queue_changed"
    moved = committed_result(engine, move_request)
    assert moved["snapshot"]["timeline_revision"] == 1

    first_id = speechicle_id(engine, first)
    voice_request = request_mutation(
        engine, "play", id=first_id, voice="bm_fable"
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    voice_changed = committed_result(engine, voice_request)
    assert voice_changed["result_id"] == first_id
    assert voice_changed["snapshot"]["timeline_revision"] == 2

    revision, fingerprint = engine.load_timeline_revision_seed()
    restarted = engine.State(revision, fingerprint)
    after_restart = engine.publish_status("playing", restarted, force=True)
    assert after_restart is not None
    assert after_restart["timeline_revision"] == 2


def test_private_mutate_command_normalizes_camel_case_and_prints_only_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = load_engine("super_speech_engine_mutate_cli")
    configure_runtime(engine, tmp_path)
    public_id = f"sp_{'1' * 32}"
    before_id = f"sp_{'2' * 32}"
    captured_requests = []
    monkeypatch.setattr(engine, "start_engine", lambda: None)

    def request(mutation) -> str:
        captured_requests.append(mutation)
        return mutation.request_id

    monkeypatch.setattr(engine, "request_mutation", request)
    monkeypatch.setattr(
        engine,
        "wait_for_mutation_result",
        lambda request_id: {
            "outcome": "committed",
            "request_id": request_id,
            "snapshot": {"version": engine.STATUS_VERSION},
        },
    )

    assert engine.cli(
        [
            "mutate",
            json.dumps(
                {
                    "type": "move",
                    "section": "waiting",
                    "id": public_id,
                    "beforeId": before_id,
                }
            ),
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "committed"
    assert len(captured_requests) == 1
    assert captured_requests[0].before_id == before_id
