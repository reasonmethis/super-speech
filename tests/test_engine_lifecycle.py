from __future__ import annotations

import json
import os
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from engine_test_support import (
    CallbackStop,
    buffered_piece,
    build_mutation,
    claim_next_speechicle,
    committed_result,
    configure_runtime,
    load_engine,
    loading_status,
    prepare_timeline,
    rejected_result,
    request_mutation,
    set_current,
    speechicle_id,
    write_storage_ready,
)

from pauseable_audio import PauseableAudio


def test_mutation_claim_accepts_replace_that_completed_before_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_claim_verified_replace")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    archived = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    archived.write_text("Archived", encoding="utf-8")
    prepare_timeline(engine)
    request_id = request_mutation(
        engine,
        "delete",
        id=speechicle_id(engine, archived),
    )
    real_replace = engine.os.replace

    def replace_then_report_error(source, destination) -> None:
        real_replace(source, destination)
        if Path(source).suffix == ".json" and Path(destination).suffix == ".claim":
            raise PermissionError("replace reported an error after commit")

    monkeypatch.setattr(engine.os, "replace", replace_then_report_error)

    state = engine.State()
    engine.process_mutation_requests(queue.Queue(), state)

    committed_result(engine, request_id)
    assert not state.stop.is_set()
    assert not archived.exists()


def test_ambiguous_mutation_claim_stops_before_applying_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_claim_ambiguous_replace")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    archived = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    archived.write_text("Archived", encoding="utf-8")
    prepare_timeline(engine)
    request_id = request_mutation(
        engine,
        "delete",
        id=speechicle_id(engine, archived),
    )

    def copy_then_report_error(source, destination) -> None:
        Path(destination).write_bytes(Path(source).read_bytes())
        raise PermissionError("claim outcome is ambiguous")

    monkeypatch.setattr(engine.os, "replace", copy_then_report_error)

    state = engine.State()
    engine.process_mutation_requests(queue.Queue(), state)

    assert state.stop.is_set()
    assert archived.exists()
    assert not engine.mutation_result_path(request_id).exists()


def test_mutation_claim_does_not_skip_a_locked_earlier_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_claim_fifo_lock")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    for _ in range(2):
        request_mutation(engine, "clear")
    pending = sorted(engine.BASE.glob("MUTATION.*.json"))
    assert len(pending) == 2
    attempted: list[Path] = []

    def block_claim(source, _destination) -> None:
        attempted.append(Path(source))
        raise PermissionError("temporarily locked")

    monkeypatch.setattr(engine.os, "replace", block_claim)

    assert engine.claim_next_mutation_request() is None
    assert attempted == pending[:1]
    assert sorted(engine.BASE.glob("MUTATION.*.json")) == pending
    assert not list(engine.BASE.glob("MUTATION.*.claim"))


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


@pytest.mark.parametrize(
    ("piece", "start", "end"),
    [(0, 0, 1), (1, -1, 1), (1, 1, 1), (1, 2, 1)],
)
def test_active_piece_rejects_impossible_progress(
    piece: int, start: int, end: int
) -> None:
    engine = load_engine(f"super_speech_engine_invalid_piece_{piece}_{start}_{end}")

    with pytest.raises(ValueError, match="invalid active piece progress"):
        engine.ActivePiece(piece, start, end)


def test_current_projection_rejects_progress_outside_its_text() -> None:
    engine = load_engine("super_speech_engine_invalid_current_progress")

    with pytest.raises(ValueError, match="active piece exceeds piece count"):
        engine.CurrentProjection(
            "speech.txt",
            "Speech",
            engine.ActivePiece(2, 0, len("Speech")),
        )
    with pytest.raises(ValueError, match="active piece exceeds Current text"):
        engine.CurrentProjection(
            "speech.txt",
            "Speech",
            engine.ActivePiece(1, 0, len("Speech") + 1),
        )


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


def test_status_exposes_pause_current_chunk_and_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status")

    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "SPLIT_CHARS", 15)
    engine.PAUSE.touch()
    current_text = "Current words. More words."
    (engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt").write_text(current_text, encoding="utf-8")
    full_queue_text = "Queued words " * 40
    for number in range(2, 7):
        (engine.QUEUE / f"{number:03d}-sp_{number:032x}-bm_fable-say.txt").write_text(
            full_queue_text if number == 2 else f"Queued words {number}",
            encoding="utf-8",
        )

    state = engine.State()
    set_current(
        engine,
        state,
        engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt",
        piece=1,
        piece_end=len("Current words."),
    )

    engine.publish_status(state, force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "paused"
    assert status["current"]["text"] == current_text
    assert status["current"]["voice"] == "af_heart"
    assert status["current"]["piece"] == 1
    assert status["current"]["piece_count"] == 2
    assert status["current"]["piece_start"] == 0
    assert status["current"]["piece_end"] == len("Current words.")
    assert status["timeline_revision"] == 0
    assert status["queue_count"] == 5
    assert len(status["queue"]) == 5
    assert status["queue"][0]["text"] == full_queue_text.strip()
    assert status["queue"][-1]["text"] == "Queued words 6"
    assert status["version"] == engine.STATUS_VERSION
    assert status["history_count"] == 0
    assert status["history"] == []


def test_status_stays_playing_while_the_current_item_waits_for_its_next_piece(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_status_between_pieces")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "SPLIT_CHARS", 15)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Two sentences. Still one speech item.", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current, piece=1, piece_end=len("Two sentences."))

    engine.publish_status(state, force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "playing"
    assert status["current"]["id"] == speechicle_id(engine, current)


def test_audio_stream_failure_propagates_before_playback(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_stream_start_failure")
    configure_runtime(engine, tmp_path)
    path = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    path.write_text("Speech", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, path)

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
    path = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
    queued = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
            buffered_piece(
                engine,
                queued,
                np.ones(4, dtype=np.float32),
                generation=generation,
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
    queued = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    queued.write_text("Still being prepared", encoding="utf-8")

    engine.publish_status(engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "playing"
    assert status["current"]["id"] == speechicle_id(engine, queued)
    assert status["current"]["piece"] == 0
    assert status["current"]["piece_start"] is None
    assert status["current"]["piece_end"] is None
    assert status["queue"] == []

    queued.unlink()
    engine.publish_status(engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "idle"
    assert status["queue"] == []


@pytest.mark.parametrize("cached_projection", ["missing", "mismatched"])
def test_status_uses_queue_first_when_cached_progress_cannot_apply(
    tmp_path: Path,
    cached_projection: str,
) -> None:
    engine = load_engine(f"super_speech_engine_status_{cached_projection}_progress")
    configure_runtime(engine, tmp_path)
    first = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    first.write_text("Durable first", encoding="utf-8")
    second.write_text("Cached second", encoding="utf-8")
    state = engine.State()
    if cached_projection == "mismatched":
        set_current(engine, state, second, piece=1)
    state.stop.set()

    status = engine.publish_status(state, force=True)

    assert status is not None
    assert status["current"]["id"] == speechicle_id(engine, first)
    assert status["current"]["text"] == "Durable first"
    assert status["current"]["piece"] == 0
    assert [item["id"] for item in status["queue"]] == [
        speechicle_id(engine, second)
    ]


def test_status_replaces_progress_that_is_not_for_queue_first(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_activation_durable_first")
    configure_runtime(engine, tmp_path)
    first = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    first.write_text("Durable first", encoding="utf-8")
    second.write_text("Stale projection", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, second, piece=1)
    state.saw_stop = True

    assert engine.publish_status(state, force=True) is not None

    assert state.current_projection is not None
    assert state.current_projection.filename == first.name
    assert state.current_projection.active_piece is None


def test_status_reports_holding_when_paused_without_speech(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_status_empty_pause")
    configure_runtime(engine, tmp_path)
    engine.PAUSE.touch()

    engine.publish_status(engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "holding"
    assert status["current"] is None
    assert status["queue"] == []


def test_status_count_matches_the_items_in_the_same_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status_read_failure")
    configure_runtime(engine, tmp_path)
    readable = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    unreadable = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    readable.write_text("Readable", encoding="utf-8")
    unreadable.write_text("Temporarily locked", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if path == unreadable:
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    engine.publish_status(engine.State(), force=True)
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
        json.dumps(
            {
                "version": engine.STATUS_VERSION,
                "timeline_revision": 0,
                "state": "stopped",
                "queue": [],
                "history": [],
            }
        ),
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
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"


def test_persistent_status_publication_failure_stops_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status_write_failure")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    state = engine.State()
    engine._status_failure_started = engine.time.monotonic() - 6
    real_replace = engine.os.replace

    def fail_replace(*_args) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(engine.os, "replace", fail_replace)

    engine.publish_status(state, force=True)

    assert state.stop.is_set()
    assert engine.STATUS_FAILURE.exists()

    monkeypatch.setattr(engine.os, "replace", real_replace)
    state.stop.clear()
    engine.publish_status(state, force=True)

    assert not engine.STATUS_FAILURE.exists()


def test_status_publication_uses_a_fresh_temporary_file_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status_fresh_temporary")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    state = engine.State()
    temporary_paths: list[Path] = []
    real_replace = engine.os.replace

    def replace(source: Path, target: Path) -> None:
        temporary_paths.append(Path(source))
        if len(temporary_paths) == 1:
            raise PermissionError("temporary file is being scanned")
        real_replace(source, target)

    monkeypatch.setattr(engine.os, "replace", replace)

    engine.publish_status(state, force=True)
    engine.publish_status(state, force=True)

    assert len(temporary_paths) == 2
    assert temporary_paths[0] != temporary_paths[1]
    assert not state.stop.is_set()
    assert json.loads(engine.STATUS.read_text(encoding="utf-8"))["state"] == "idle"
    log = engine.LOG.read_text(encoding="utf-8")
    assert "status publication failed: PermissionError" in log
    assert "status publication recovered" in log


def test_backward_wall_clock_cannot_throttle_or_regress_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_backward_status_clock")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current progress", encoding="utf-8")
    state = engine.State()
    prepare_timeline(engine)
    monotonic_times = iter([10.0, 10.1, 10.3])
    wall_times = iter([100.0, 80.0])
    monkeypatch.setattr(engine.time, "monotonic", lambda: next(monotonic_times))
    monkeypatch.setattr(engine.time, "time", lambda: next(wall_times))

    initial = engine.publish_status(state, force=True)
    assert initial is not None
    assert engine.update_current_piece(
        state,
        current.name,
        1,
        4,
        len("Current progress"),
    )

    assert engine.publish_status(state) is None
    progress = engine.publish_status(state)

    assert progress is not None
    assert progress["current"]["piece"] == 1
    assert initial["updated_at"] == 100.0
    assert progress["updated_at"] > initial["updated_at"]


def test_stopped_status_contains_the_same_queue_items_as_its_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = load_engine("super_speech_engine_stopped_status")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")

    engine.print_status()
    status = json.loads(capsys.readouterr().out)

    assert status["state"] == "stopped"
    assert status["current"]["id"] == speechicle_id(engine, waiting)
    assert status["queue_count"] == len(status["queue"]) == 0


def test_enqueue_text_reserves_the_next_queue_number(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_enqueue")

    configure_runtime(engine, tmp_path)
    (engine.SPOKEN / "007-sp_00000000000000000000000000000007-af_heart-say.txt").write_text("Earlier", encoding="utf-8")
    prepare_timeline(engine)

    queued = engine.enqueue_text("New words", "bm_fable", 650)

    assert engine.SpeechicleFilename.parse(queued.name).sequence == 8
    assert engine.voice_from_name(queued.name) == "bm_fable"
    assert engine.gap_from_name(queued.name) == 0.65
    assert queued.read_text(encoding="utf-8") == "New words"


def test_enqueue_publishes_the_final_queue_path_only_after_writing_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_enqueue_atomic")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
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
    first = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_bella-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")

    engine.timeline.save_queue_order([third, first, second])
    prepare_timeline(engine)
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
    first = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    engine.timeline.save_queue_order([second, first])
    original_read_text = Path.read_text

    def locked_order(path: Path, *args, **kwargs) -> str:
        if path == engine.timeline.paths.queue_order:
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked_order)

    assert claim_next_speechicle(engine, engine.State()) == second


def test_voice_rename_recovery_keeps_the_saved_queue_position(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_voice_rename_recovery")
    configure_runtime(engine, tmp_path)
    first = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_queue_order([third, first, second])
    renamed = engine.QUEUE / "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    os.replace(first, renamed)

    assert engine.queue_files_in_order() == [third, renamed, second]


def test_failed_sequence_numbers_are_not_reused(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_failed_sequence")
    configure_runtime(engine, tmp_path)
    engine.FAILED.mkdir()
    (engine.FAILED / "007-sp_00000000000000000000000000000007-af_heart-say.txt").write_text("Failed", encoding="utf-8")
    prepare_timeline(engine)

    assert engine.timeline.sequence(engine.enqueue_text("New", "af_heart")) == 8


def test_moving_a_waiting_chunk_resets_banked_audio_but_keeps_current(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_queue_move")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_bella-say.txt"
    for path in (current, second, third):
        path.write_text(path.stem, encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put(buffered_piece(engine, current, "current piece"))
    buffered.put(buffered_piece(engine, second, "stale second"))
    buffered.put(buffered_piece(engine, third, "stale third"))
    state = engine.State()
    set_current(engine, state, current)
    generations: dict[str, int] = {}
    for path in (current, second, third):
        _, generations[path.name] = engine._record_claim(state, path)

    engine.apply_waiting_move_mutation(
        buffered,
        state,
        build_mutation(
            engine,
            "move",
            section="waiting",
            id=speechicle_id(engine, third),
            before_id=speechicle_id(engine, second),
        ),
    )

    assert [path.stem for path in engine.queue_files_in_order()] == [
        current.stem,
        third.stem,
        second.stem,
    ]
    kept = buffered.get_nowait()
    assert kept.path == current
    assert kept.audio == "current piece"
    assert buffered.empty()
    assert state.claims == {current.name: generations[current.name]}
    assert claim_next_speechicle(engine, state) == third


def test_reset_waiting_buffer_keeps_queue_first_claim_without_cached_progress(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_reset_without_projection")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put(buffered_piece(engine, current, "current piece"))
    buffered.put(buffered_piece(engine, waiting, "waiting piece"))
    state = engine.State()
    _, current_generation = engine._record_claim(state, current)
    engine._record_claim(state, waiting)

    assert engine._reset_waiting_buffer(buffered, state, current.name) == 1

    kept = buffered.get_nowait()
    assert kept.path == current
    assert kept.audio == "current piece"
    assert buffered.empty()
    assert state.claims == {current.name: current_generation}


@pytest.mark.parametrize("action", ["move", "archive"])
def test_waiting_mutation_does_not_reread_storage_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    engine = load_engine(f"super_speech_engine_post_commit_reset_{action}")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_bella-say.txt"
    for path in (current, second, third):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    engine._record_claim(state, current)
    original_queue_files = engine.queue_files_in_order

    def fail_disk_read() -> list[Path]:
        raise RuntimeError("injected post-commit disk read")

    if action == "archive":
        original_commit = engine.archive

        def commit_archive(path: Path) -> bool:
            committed = original_commit(path)
            monkeypatch.setattr(engine, "queue_files_in_order", fail_disk_read)
            return committed

        monkeypatch.setattr(engine, "archive", commit_archive)
        request = build_mutation(
            engine,
            "archive",
            id=speechicle_id(engine, second),
        )
        engine.apply_archive_mutation(queue.Queue(), state, request)
    else:
        original_commit = engine.timeline.save_queue_order

        def commit_move(paths: list[Path]) -> None:
            original_commit(paths)
            monkeypatch.setattr(engine, "queue_files_in_order", fail_disk_read)

        monkeypatch.setattr(engine.timeline, "save_queue_order", commit_move)
        request = build_mutation(
            engine,
            "move",
            section="waiting",
            id=speechicle_id(engine, third),
            before_id=speechicle_id(engine, second),
        )
        engine.apply_waiting_move_mutation(queue.Queue(), state, request)

    monkeypatch.setattr(engine, "queue_files_in_order", original_queue_files)
    if action == "archive":
        assert not second.exists()
        assert (engine.SPOKEN / second.name).exists()
    else:
        assert engine.queue_files_in_order() == [current, third, second]


@pytest.mark.parametrize("action", ["move", "archive"])
def test_post_commit_waiting_reset_failure_is_never_reported_as_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    engine = load_engine(f"super_speech_engine_waiting_unconfirmed_{action}")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_bella-say.txt"
    for path in (current, second, third):
        path.write_text(path.stem, encoding="utf-8")
    if action == "archive":
        request_id = request_mutation(
            engine,
            "archive",
            id=speechicle_id(engine, second),
        )
    else:
        request_id = request_mutation(
            engine,
            "move",
            section="waiting",
            id=speechicle_id(engine, third),
            before_id=speechicle_id(engine, second),
        )
    monkeypatch.setattr(
        engine,
        "_reset_waiting_buffer",
        lambda *_args: (_ for _ in ()).throw(OSError("injected reset failure")),
    )
    state = engine.State()

    engine.process_mutation_requests(queue.Queue(), state)

    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "unconfirmed"
    assert state.stop.is_set()
    if action == "archive":
        assert not second.exists()
        assert (engine.SPOKEN / second.name).exists()
    else:
        assert engine.queue_files_in_order() == [current, third, second]


def test_waiting_move_temp_write_failure_is_rejected_without_stopping_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_move_precommit_failure")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_bella-say.txt"
    for path in (current, second, third):
        path.write_text(path.stem, encoding="utf-8")
    request_id = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, third),
        before_id=speechicle_id(engine, second),
    )
    original_write = Path.write_text

    def fail_order_temp(path: Path, *args, **kwargs) -> int:
        if path.parent == engine.BASE and path.name.startswith(
            f"{engine.timeline.paths.queue_order.name}."
        ):
            raise PermissionError("injected pre-replace failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_order_temp)
    state = engine.State()

    engine.process_mutation_requests(queue.Queue(), state)

    result = rejected_result(engine, request_id)
    assert "injected pre-replace failure" in str(result["error"])
    assert not state.stop.is_set()
    assert engine.queue_files_in_order() == [current, second, third]


def test_waiting_move_accepts_verified_replace_success_after_replace_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_move_verified_replace")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_bella-say.txt"
    for path in (current, second, third):
        path.write_text(path.stem, encoding="utf-8")
    request_id = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, third),
        before_id=speechicle_id(engine, second),
    )
    original_replace = engine.os.replace

    def replace_then_report_error(source, destination) -> None:
        original_replace(source, destination)
        if Path(destination) == engine.timeline.paths.queue_order:
            raise PermissionError("ambiguous replace result")

    monkeypatch.setattr(engine.os, "replace", replace_then_report_error)

    engine.process_mutation_requests(queue.Queue(), engine.State())

    committed_result(engine, request_id)
    assert engine.queue_files_in_order() == [current, third, second]


@pytest.mark.parametrize("action", ["move", "archive"])
def test_waiting_mutation_preserves_mid_speechicle_piece_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    engine = load_engine(f"super_speech_engine_mid_piece_{action}")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    moved = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (current, waiting, moved):
        path.write_text(path.stem, encoding="utf-8")
    monkeypatch.setattr(engine, "SPLIT_CHARS", 5)
    current.write_text("One. Two. Three. Four. Five.", encoding="utf-8")
    held_piece = buffered_piece(engine, current, "piece 3", piece=3)
    remaining_pieces = [
        buffered_piece(engine, current, "piece 4", first=False, piece=4),
        buffered_piece(engine, current, "piece 5", first=False, piece=5),
    ]
    buffered: queue.Queue = queue.Queue()
    for entry in [
        *remaining_pieces,
        buffered_piece(engine, waiting, "stale"),
        buffered_piece(engine, moved, "stale"),
    ]:
        buffered.put(entry)
    state = engine.State()
    set_current(engine, state, current, piece=2, piece_start=5, piece_end=9)
    _, current_generation = engine._record_claim(state, current)
    engine._record_claim(state, waiting)
    engine._record_claim(state, moved)
    if action == "move":
        request_id = request_mutation(
            engine,
            "move",
            section="waiting",
            id=speechicle_id(engine, moved),
            before_id=speechicle_id(engine, waiting),
        )
    else:
        request_id = request_mutation(
            engine, "archive", id=speechicle_id(engine, waiting)
        )

    assert (
        engine.process_mutation_requests(buffered, state, held_piece.path.name)
        == "queue_changed"
    )

    committed_result(engine, request_id)
    assert held_piece.audio == "piece 3"
    assert [buffered.get_nowait(), buffered.get_nowait()] == remaining_pieces
    assert buffered.empty()
    assert state.current_projection is not None
    assert state.current_projection.active_piece is not None
    assert state.current_projection.active_piece.piece == 2
    assert state.claims == {current.name: current_generation}
    assert not engine.buffered_piece_is_stale(
        state, current.name, current_generation
    )


def test_archiving_one_waiting_chunk_preserves_current_and_remaining_queue(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_queue_archive")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    archived = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    remaining = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_bella-say.txt"
    for path in (current, archived, remaining):
        path.write_text(path.stem, encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put(buffered_piece(engine, current, "current piece"))
    buffered.put(buffered_piece(engine, archived, "stale archived"))
    buffered.put(buffered_piece(engine, remaining, "stale remaining"))
    state = engine.State()
    set_current(engine, state, current)
    for path in (current, archived, remaining):
        engine._record_claim(state, path)

    engine.apply_archive_mutation(
        buffered,
        state,
        build_mutation(engine, "archive", id=speechicle_id(engine, archived)),
    )

    assert not archived.exists()
    assert (engine.SPOKEN / archived.name).is_file()
    assert [path.stem for path in engine.queue_files_in_order()] == [
        current.stem,
        remaining.stem,
    ]
    kept = buffered.get_nowait()
    assert kept.path == current
    assert kept.audio == "current piece"
    assert set(state.claims) == {current.name}


def test_waiting_mutation_rejects_queue_first_without_a_projection(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_first_queue_mutation")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()

    with pytest.raises(ValueError, match="Speechicle not found in Waiting"):
        engine.apply_archive_mutation(
            queue.Queue(),
            state,
            build_mutation(engine, "archive", id=speechicle_id(engine, current)),
        )

    assert engine.queue_files_in_order() == [current, waiting]


def test_deleting_one_history_chunk_preserves_waiting_queue(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_delete")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    history = engine.SPOKEN / "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    history.write_text("History", encoding="utf-8")
    assert engine.history_snapshot()[0] == 1

    engine.apply_delete_mutation(
        build_mutation(engine, "delete", id=speechicle_id(engine, history))
    )

    assert waiting.read_text(encoding="utf-8") == "Waiting"
    assert not history.exists()
    assert engine.history_snapshot() == (0, [])


def test_deleting_an_already_absent_history_chunk_is_idempotent(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_delete_absent")
    configure_runtime(engine, tmp_path)

    engine.apply_delete_mutation(
        build_mutation(engine, "delete", id=f"sp_{'1' * 32}")
    )

    assert engine.history_snapshot() == (0, [])


def test_history_delete_is_rejected_while_the_same_item_is_active(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_history_delete_active")
    configure_runtime(engine, tmp_path)
    queued = engine.QUEUE / "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    history = engine.SPOKEN / queued.name
    queued.write_text("Active replay", encoding="utf-8")
    history.write_text("Active replay", encoding="utf-8")
    prepare_timeline(engine)

    with pytest.raises(ValueError, match="Speechicle is Current or Waiting"):
        engine.apply_delete_mutation(
            build_mutation(engine, "delete", id=speechicle_id(engine, queued))
        )

    assert queued.exists()
    assert not history.exists()


def test_history_delete_does_not_depend_on_rewriting_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_delete_order_failure")
    configure_runtime(engine, tmp_path)
    history = engine.SPOKEN / "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    history.write_text("History", encoding="utf-8")
    assert engine.history_snapshot()[0] == 1

    def fail_order_update(*_args) -> None:
        raise AssertionError("delete should not rewrite History order")

    monkeypatch.setattr(engine.timeline, "_write_order", fail_order_update)

    engine.apply_delete_mutation(
        build_mutation(engine, "delete", id=speechicle_id(engine, history))
    )

    assert not history.exists()
    assert engine.history_snapshot() == (0, [])


def test_archive_waits_for_history_order_before_moving_the_current_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_order_fallback")
    configure_runtime(engine, tmp_path)
    older = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    newer = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    current = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (older, newer, current):
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_history_order([newer, older])

    real_write = engine.timeline._write_order

    def fail_history_order(path: Path, ids: list[str]) -> None:
        if path == engine.timeline.paths.history_order:
            raise PermissionError("locked")
        real_write(path, ids)

    monkeypatch.setattr(engine.timeline, "_write_order", fail_history_order)

    assert not engine.archive(current)
    assert current.exists()
    assert engine.timeline.history_files() == [newer, older]


def test_waiting_move_publishes_a_committed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_request")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    first = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    second = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    first.write_text("First waiting", encoding="utf-8")
    second.write_text("Second waiting", encoding="utf-8")

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
        current.stem,
        second.stem,
        first.stem,
    ]


def test_waiting_move_before_itself_does_not_reset_buffered_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_noop_move")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put(buffered_piece(engine, current, "current piece"))
    buffered.put(buffered_piece(engine, waiting, "waiting piece"))
    state = engine.State()
    engine._record_claim(state, current)
    engine._record_claim(state, waiting)
    waiting_id = speechicle_id(engine, waiting)
    request_id = request_mutation(
        engine,
        "move",
        section="waiting",
        id=waiting_id,
        before_id=waiting_id,
    )

    assert engine.process_mutation_requests(buffered, state) is None
    committed_result(engine, request_id)
    assert engine.queue_files_in_order() == [current, waiting]
    assert [buffered.get_nowait().audio, buffered.get_nowait().audio] == [
        "current piece",
        "waiting piece",
    ]


def test_timed_out_unclaimed_queue_request_cannot_apply_later(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_timeout")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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


def test_timeout_accepts_cancellation_that_completed_before_replace_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_cancel_verified_replace")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = request_mutation(engine, "delete", id=f"sp_{'1' * 32}")
    real_replace = engine.os.replace

    def replace_then_report_error(source, destination) -> None:
        real_replace(source, destination)
        if Path(destination).suffix == ".cancel":
            raise PermissionError("replace reported an error after cancellation")

    monkeypatch.setattr(engine.os, "replace", replace_then_report_error)

    with pytest.raises(RuntimeError, match="did not publish"):
        engine.wait_for_mutation_result(request_id, timeout=0.01)

    assert not list(engine.BASE.glob(f"MUTATION.*.{request_id}.*"))


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
    snapshot = engine.publish_status(state, force=True)
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
    history = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
    snapshot = engine.publish_status(engine.State(), force=True)
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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)

    request_id = request_mutation(
        engine, "archive", id=speechicle_id(engine, current)
    )
    assert engine.process_mutation_requests(queue.Queue(), state) is None

    result = rejected_result(engine, request_id)
    assert "Speechicle not found in Waiting" in str(result["error"])
    assert current.is_file()


def test_unconfirmed_waiting_archive_stops_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_archive_unconfirmed")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
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
    prepare_timeline(engine)

    queued = engine.enqueue_text("New words", "af_heart")

    assert engine.timeline.sequence(queued) == 1
    assert engine.voice_from_name(queued.name) == "af_heart"


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
    queued = tmp_path / "001-sp_00000000000000000000000000000001-af_heart-g300-say.txt"
    calls: list[object] = []

    monkeypatch.setattr(engine, "start_engine", lambda: calls.append("start"))

    def enqueue(
        text: str,
        voice: str,
        gap_ms: int | None,
        source: str | None,
        inbox: str | None,
    ) -> Path:
        calls.append((text, voice, gap_ms, source, inbox))
        return queued

    monkeypatch.setattr(engine, "enqueue_text", enqueue)
    public_id = f"sp_{'1' * 32}"
    monkeypatch.setattr(engine, "public_id_for_path", lambda _path: public_id)
    monkeypatch.setattr(
        engine, "wait_for_queue_acceptance", lambda: calls.append("accept")
    )

    assert engine.cli(
        ["speak", "Hello there", "--voice", r"af\_heart", "--gap-ms", "300"]
    ) == 0
    assert calls == ["start", ("Hello there", "af_heart", 300, None, None), "accept"]
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
        json.dumps(loading_status(engine, 1111)),
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
            json.dumps(loading_status(engine, fake_pid, engine.time.time() + 1)),
            encoding="utf-8",
        )
        write_storage_ready(engine, fake_pid)

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
        json.dumps(loading_status(engine, 4321)),
        encoding="utf-8",
    )
    write_storage_ready(engine, 4321)

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


def test_speak_waits_for_delayed_storage_preparation_and_enqueues_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_delayed_storage_ready")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    engine.STATUS.write_text(
        json.dumps(loading_status(engine, os.getpid(), engine.time.time())),
        encoding="utf-8",
    )
    readiness_checked = threading.Event()
    real_storage_is_ready = engine.storage_is_ready

    def observe_readiness(engine_pid: object) -> bool:
        readiness_checked.set()
        return real_storage_is_ready(engine_pid)

    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    monkeypatch.setattr(engine, "storage_is_ready", observe_readiness)
    monkeypatch.setattr(engine, "wait_for_queue_acceptance", lambda: True)
    results: list[int] = []
    errors: list[BaseException] = []

    def speak() -> None:
        try:
            results.append(engine.cli(["speak", "Prepared exactly once"]))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=speak)
    worker.start()
    assert readiness_checked.wait(timeout=1)
    assert worker.is_alive()
    assert not list(engine.QUEUE.glob("*.txt"))

    write_storage_ready(engine, os.getpid())
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert results == [0]
    queued = list(engine.QUEUE.glob("*.txt"))
    assert len(queued) == 1
    assert queued[0].read_text(encoding="utf-8") == "Prepared exactly once"


def test_process_exists_recognizes_the_current_process() -> None:
    engine = load_engine("super_speech_engine_process_exists")

    assert engine.process_exists(os.getpid())


def test_start_engine_ignores_status_from_a_previous_lock_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_existing_stale_owner")
    configure_runtime(engine, tmp_path)
    engine.STATUS.write_text(
        json.dumps(loading_status(engine, 1111, 1)),
        encoding="utf-8",
    )
    write_storage_ready(engine, 1111)
    sleeps = 0

    def publish_current_owner(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        engine.STATUS.write_text(
            json.dumps(loading_status(engine, 2222, 2)),
            encoding="utf-8",
        )
        write_storage_ready(engine, 2222)

    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    monkeypatch.setattr(engine, "process_exists", lambda process_id: process_id == 2222)
    monkeypatch.setattr(engine.time, "sleep", publish_current_owner)
    monkeypatch.setattr(
        engine.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("must not launch a second engine"),
    )

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
    monkeypatch.setattr(engine, "wait_for_engine_status", lambda **_kwargs: False)
    monkeypatch.setattr(engine, "process_exists", lambda process_id: process_id == 4321)

    with pytest.raises(
        RuntimeError,
        match=rf"unsupported protocol version {engine.STATUS_VERSION - 1}",
    ):
        engine.start_engine()


def test_start_engine_retries_after_stopped_status_releases_the_instance_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_status_reader_lock")
    configure_runtime(engine, tmp_path)
    engine.MODEL_PATH = tmp_path / "model.onnx"
    engine.VOICES_PATH = tmp_path / "voices.bin"
    engine.MODEL_PATH.touch()
    engine.VOICES_PATH.touch()
    engine.STATUS.write_text(
        json.dumps(
            {
                **loading_status(engine, 1111),
                "version": engine.STATUS_VERSION - 1,
            }
        ),
        encoding="utf-8",
    )
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_for_stopped_status() -> None:
        lock = engine.EngineInstanceLock()
        assert lock.acquire()
        holder_ready.set()
        release_holder.wait(timeout=2)
        lock.release()

    holder = threading.Thread(target=hold_for_stopped_status)
    holder.start()
    assert holder_ready.wait(timeout=2)
    launched_lock = engine.EngineInstanceLock()
    launched = False
    real_sleep = engine.time.sleep

    class FakeProcess:
        pid = 2222

        @staticmethod
        def poll() -> None:
            return None

    def launch(*_args, **_kwargs) -> FakeProcess:
        nonlocal launched
        launched = True
        assert launched_lock.acquire()
        engine.STATUS.write_text(
            json.dumps(loading_status(engine, FakeProcess.pid, engine.time.time())),
            encoding="utf-8",
        )
        write_storage_ready(engine, FakeProcess.pid)
        return FakeProcess()

    def let_status_finish(_seconds: float) -> None:
        release_holder.set()
        real_sleep(0.01)

    monkeypatch.setattr(engine.subprocess, "Popen", launch)
    monkeypatch.setattr(engine.time, "sleep", let_status_finish)
    try:
        engine.start_engine()
    finally:
        release_holder.set()
        holder.join(timeout=2)
        launched_lock.release()

    assert launched
    assert not holder.is_alive()


def test_pause_and_resume_commands_share_the_runtime_signal(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_pause")
    configure_runtime(engine, tmp_path)

    assert engine.cli(["pause"]) == 0
    assert engine.PAUSE.is_file()
    assert engine.cli(["resume"]) == 0
    assert not engine.PAUSE.exists()


def test_playback_command_sequence_persists_across_engine_reload(
    tmp_path: Path,
) -> None:
    first = load_engine("super_speech_engine_command_sequence_first")
    configure_runtime(first, tmp_path)

    assert first.publish_ordered_marker(first.PAUSE) == 1
    assert first.resume() is None

    restarted = load_engine("super_speech_engine_command_sequence_restarted")
    configure_runtime(restarted, tmp_path)
    assert restarted.publish_ordered_marker(restarted.STOP) == 3

    marker = json.loads(restarted.STOP.read_text(encoding="utf-8"))
    counter = json.loads(
        restarted.PLAYBACK_COMMAND_SEQUENCE.read_text(encoding="utf-8")
    )
    assert marker["command_sequence"] == 3
    assert counter == {"version": 1, "last_sequence": 3}


def test_invalid_playback_command_sequence_stops_new_publication(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_invalid_command_sequence")
    configure_runtime(engine, tmp_path)
    engine.PLAYBACK_COMMAND_SEQUENCE.write_text("{", encoding="utf-8")

    with pytest.raises(RuntimeError, match="sequence is unavailable"):
        engine.publish_ordered_marker(engine.PAUSE)

    assert not engine.PAUSE.exists()
    assert engine.PLAYBACK_COMMAND_SEQUENCE.read_text(encoding="utf-8") == "{"


def test_missing_counter_recovers_pending_commands_before_allocating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_recovered_command_sequence")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    assert engine.publish_ordered_marker(engine.PAUSE) == 1
    first_id = request_mutation(engine, "clear")
    second_id = request_mutation(engine, "clear")
    engine.PLAYBACK_COMMAND_SEQUENCE.unlink()

    assert engine.publish_ordered_marker(engine.CONTINUE) == 4

    pending = {
        json.loads(path.read_text(encoding="utf-8"))["command_sequence"]
        for path in engine.BASE.glob("MUTATION.*.json")
    }
    assert pending == {2, 3}
    assert {first_id, second_id} == {
        path.name.split(".")[-2]
        for path in engine.BASE.glob("MUTATION.*.json")
    }
    marker = json.loads(engine.CONTINUE.read_text(encoding="utf-8"))
    assert marker["command_sequence"] == 4


def test_clear_publishes_one_mutation_without_a_pause_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_immediate_clear")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)

    request_id = request_mutation(engine, "clear")

    request_path = next(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert not engine.PAUSE.exists()
    assert request["command_sequence"] == 1

    assert engine.process_mutation_requests(queue.Queue(), state) == "clear"
    committed_result(engine, request_id)
    assert not engine.PAUSE.exists()


@pytest.mark.parametrize("already_paused", [False, True])
def test_failed_clear_publication_preserves_the_previous_pause_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    already_paused: bool,
) -> None:
    engine = load_engine("super_speech_engine_failed_clear_publication")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    if already_paused:
        engine.publish_ordered_marker(engine.PAUSE)
    original_replace = engine._replace_command_json_unlocked

    def fail_mutation_publish(
        temporary: Path,
        target: Path,
        payload: dict[str, object],
        error_message: str,
    ) -> None:
        if target.name.startswith("MUTATION."):
            raise RuntimeError("could not publish timeline mutation")
        original_replace(temporary, target, payload, error_message)

    monkeypatch.setattr(engine, "_replace_command_json_unlocked", fail_mutation_publish)

    with pytest.raises(RuntimeError, match="could not publish timeline mutation"):
        request_mutation(engine, "clear")

    assert engine.PAUSE.exists() is already_paused
    assert not list(engine.BASE.glob("MUTATION.*.json"))


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        "{}",
        '{"command_sequence":0}',
        '{"engine_pid":null}',
        '{"engine_pid":1,"unexpected":true}',
    ],
)
def test_malformed_ordered_marker_fails_closed(
    tmp_path: Path,
    payload: str,
) -> None:
    engine = load_engine("super_speech_engine_invalid_ordered_marker")
    configure_runtime(engine, tmp_path)
    engine.STOP.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match="STOP marker"):
        engine.ordered_marker_requested(engine.STOP)


def test_ordered_marker_read_retries_a_transient_storage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_transient_marker_read")
    configure_runtime(engine, tmp_path)
    engine.STOP.write_text("", encoding="utf-8")
    original_read = Path.read_text
    attempts = 0

    def transient_read(path: Path, *args, **kwargs) -> str:
        nonlocal attempts
        if path == engine.STOP and attempts < 2:
            attempts += 1
            raise PermissionError("temporarily locked")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient_read)

    assert engine.ordered_marker_requested(engine.STOP)
    assert attempts == 2


def test_unreadable_stop_prevents_destructive_mutation_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_unreadable_stop")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    request_mutation(engine, "clear")
    engine.publish_ordered_marker(engine.STOP)
    original_read = Path.read_text

    def unreadable_stop(path: Path, *args, **kwargs) -> str:
        if path == engine.STOP:
            raise PermissionError("locked")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_stop)

    with pytest.raises(RuntimeError, match="STOP marker is unavailable"):
        engine.process_mutation_requests(queue.Queue(), engine.State())

    assert current.exists()
    assert not (engine.SPOKEN / current.name).exists()


def test_concurrent_mutation_publishers_receive_unique_ordered_sequences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_concurrent_command_sequence")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    barrier = threading.Barrier(8)
    failures: list[Exception] = []

    def publish() -> None:
        try:
            barrier.wait()
            request_mutation(engine, "clear")
        except Exception as error:
            failures.append(error)

    publishers = [threading.Thread(target=publish) for _ in range(8)]
    for publisher in publishers:
        publisher.start()
    for publisher in publishers:
        publisher.join(2)

    assert not failures
    assert all(not publisher.is_alive() for publisher in publishers)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in engine.BASE.glob("MUTATION.*.json")
    ]
    assert sorted(payload["command_sequence"] for payload in payloads) == list(
        range(1, 9)
    )


@pytest.mark.parametrize("replace_before_error", [False, True])
def test_mutation_publication_handles_transient_windows_replace_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replace_before_error: bool,
) -> None:
    engine = load_engine(
        f"super_speech_engine_command_replace_{replace_before_error}"
    )
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    real_replace = engine.os.replace
    failed_targets: set[Path] = set()

    def transient_replace(source: Path, target: Path) -> None:
        target = Path(target)
        if target not in failed_targets:
            failed_targets.add(target)
            if replace_before_error:
                real_replace(source, target)
            raise PermissionError("temporarily locked")
        real_replace(source, target)

    monkeypatch.setattr(engine.os, "replace", transient_replace)

    request_id = request_mutation(engine, "clear")

    requests = list(engine.BASE.glob("MUTATION.*.json"))
    assert len(requests) == 1
    payload = json.loads(requests[0].read_text(encoding="utf-8"))
    assert payload["request_id"] == request_id
    assert payload["command_sequence"] == 1

    marker_sequence = engine.publish_ordered_marker(engine.PAUSE)
    marker = json.loads(engine.PAUSE.read_text(encoding="utf-8"))
    assert marker_sequence == 2
    assert marker == {"command_sequence": 2}


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
    selected = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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

def test_startup_cleanup_removes_protocol_11_request_artifacts(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_remove_v11_requests")
    configure_runtime(engine, tmp_path)
    legacy_play = engine.BASE / "PLAY.json"
    legacy_play.write_text('{"id":"001-af_heart-say"}', encoding="utf-8")
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

    assert not legacy_play.exists()
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
    engine.publish_status(state, force=True)
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
    history = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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


def test_serve_publishes_loading_status_before_delayed_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_serve_status")
    configure_runtime(engine, tmp_path)
    prior = loading_status(engine, 123, updated_at=10)
    prior["state"] = "stopped"
    prior["timeline_revision"] = 7
    prior["engine_pid"] = None
    prior["current"] = {
        "id": f"sp_{'1' * 32}",
        "text": "Current",
        "voice": "af_heart",
        "piece": 0,
        "piece_count": 1,
        "piece_start": None,
        "piece_end": None,
        "elapsed_seconds": 0.0,
    }
    engine.STATUS.write_text(json.dumps(prior), encoding="utf-8")
    observed: list[tuple[str, int]] = []

    class FakeLock:
        @staticmethod
        def acquire() -> bool:
            return True

        @staticmethod
        def release() -> None:
            pass

        held = True

    monkeypatch.setattr(engine, "EngineInstanceLock", FakeLock)

    def delayed_prepare(_lock) -> None:
        loading = json.loads(engine.STATUS.read_text(encoding="utf-8"))
        assert loading["state"] == "loading"
        assert loading["engine_pid"] == os.getpid()
        assert loading["current"] == prior["current"]
        assert engine.HEARTBEAT.exists()
        assert not engine.STORAGE_READY.exists()
        observed.append((loading["state"], loading["timeline_revision"]))

    def run_loop(revision: int, seed: str) -> None:
        assert engine.storage_is_ready(os.getpid())
        observed.append((seed, revision))

    def clear_startup_signals() -> None:
        assert not engine.STORAGE_READY.exists()

    fingerprint = engine.fingerprint_from_status(prior)
    assert fingerprint is not None
    monkeypatch.setattr(engine, "prepare_timeline_storage", delayed_prepare)
    monkeypatch.setattr(engine, "clear_transient_signals", clear_startup_signals)
    monkeypatch.setattr(engine, "run_engine_loop", run_loop)

    engine.serve()

    assert observed == [("loading", 7), (fingerprint, 7)]
    assert not engine.STORAGE_READY.exists()


def test_control_sent_during_loading_survives_startup_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_loading_control")
    configure_runtime(engine, tmp_path)
    preparation_started = threading.Event()
    finish_preparation = threading.Event()
    observed: list[bool] = []

    class FakeLock:
        held = True

        @staticmethod
        def acquire() -> bool:
            return True

        @staticmethod
        def release() -> None:
            pass

    def delayed_prepare(_lock) -> None:
        preparation_started.set()
        assert finish_preparation.wait(2)

    def run_loop(_revision: int, _seed: str | None) -> None:
        observed.append(engine.SKIP.exists())

    monkeypatch.setattr(engine, "EngineInstanceLock", FakeLock)
    monkeypatch.setattr(engine, "prepare_timeline_storage", delayed_prepare)
    monkeypatch.setattr(engine, "run_engine_loop", run_loop)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    monkeypatch.setattr(engine, "process_exists", lambda _pid: True)
    engine.SKIP.write_text("stale", encoding="utf-8")

    serving = threading.Thread(target=engine.serve)
    serving.start()
    assert preparation_started.wait(1)
    assert not engine.SKIP.exists()
    loading = json.loads(engine.STATUS.read_text(encoding="utf-8"))
    assert loading["state"] == "loading"

    engine.send_control(engine.SKIP)
    finish_preparation.set()
    serving.join(2)

    assert not serving.is_alive()
    assert observed == [True]


def test_startup_publications_retry_transient_windows_replace_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_startup_publication_retry")
    configure_runtime(engine, tmp_path)
    attempts = {engine.STATUS: 0, engine.STORAGE_READY: 0}
    real_replace = engine.os.replace

    def replace(source: Path, target: Path) -> None:
        target = Path(target)
        if target in attempts:
            attempts[target] += 1
            if attempts[target] == 1:
                raise PermissionError("temporarily locked")
        real_replace(source, target)

    monkeypatch.setattr(engine.os, "replace", replace)

    status = engine.publish_startup_status(4)
    engine.publish_storage_ready()

    assert attempts == {engine.STATUS: 2, engine.STORAGE_READY: 2}
    assert json.loads(engine.STATUS.read_text(encoding="utf-8")) == status
    assert engine.storage_is_ready(os.getpid())


def test_status_exposes_bounded_recent_history(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_status")
    configure_runtime(engine, tmp_path)
    engine.HISTORY_LIMIT = 2
    for number in (1, 2, 3):
        (engine.SPOKEN / f"{number:03d}-sp_{number:032x}-af_heart-say.txt").write_text(
            f"History {number}", encoding="utf-8"
        )

    engine.publish_status(engine.State(), force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["version"] == engine.STATUS_VERSION
    assert status["history_count"] == 3
    assert [item["text"] for item in status["history"]] == ["History 3", "History 2"]
