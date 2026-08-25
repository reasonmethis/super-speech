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
    engine.PLAY = tmp_path / "PLAY.json"
    engine.QUEUE_COMMAND = tmp_path / "QUEUE_COMMAND.json"
    engine.QUEUE_ORDER = tmp_path / "queue-order.json"
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

    engine.publish_status("playing", state, force=True)
    status = json.loads(engine.STATUS.read_text(encoding="utf-8"))

    assert status["state"] == "paused"
    assert status["current"]["text"] == "Current words"
    assert status["current"]["voice"] == "af_heart"
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


def test_enqueue_text_reserves_the_next_queue_number(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_enqueue")

    configure_runtime(engine, tmp_path)
    (engine.SPOKEN / "007-af_heart-say.txt").write_text("Earlier", encoding="utf-8")

    queued = engine.enqueue_text("New words", "bm_fable", 650)

    assert queued.name == "008-bm_fable-g650-say.txt"
    assert queued.read_text(encoding="utf-8") == "New words"


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
    state.selection_name = third.name
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
    queued = tmp_path / "001-af_heart-g300-say.txt"
    calls: list[object] = []

    monkeypatch.setattr(engine, "start_engine", lambda: calls.append("start"))

    def enqueue(text: str, voice: str, gap_ms: int | None) -> Path:
        calls.append((text, voice, gap_ms))
        return queued

    monkeypatch.setattr(engine, "enqueue_text", enqueue)

    assert engine.cli(["speak", "Hello there", "--gap-ms", "300"]) == 0
    assert calls == ["start", ("Hello there", "af_heart", 300)]
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


def test_play_requests_acknowledge_a_superseded_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_signal")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    first_request_id = engine.request_play("001-af_heart-say")
    second_request_id = engine.request_play("002-bm_fable-say")

    assert engine.take_play_request() == ("002-bm_fable-say", second_request_id)
    assert engine.take_play_request() is None
    with pytest.raises(RuntimeError, match="superseded by a newer play request"):
        engine.wait_for_play_ack(first_request_id, timeout=0.1)


def test_play_command_starts_engine_then_publishes_the_requested_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_cli")
    configure_runtime(engine, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(engine, "start_engine", lambda: calls.append("start"))
    def request(chunk_id: str) -> str:
        calls.append(chunk_id)
        return "a" * 24

    monkeypatch.setattr(engine, "request_play", request)
    monkeypatch.setattr(
        engine,
        "wait_for_play_ack",
        lambda request_id: calls.append(("wait", request_id))
        or {"id": "007-bm_fable-say", "accepted_at": 1.0},
    )

    assert engine.cli(["play", "007-bm_fable-say"]) == 0
    assert calls == ["start", "007-bm_fable-say", ("wait", "a" * 24)]


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


def test_selecting_upcoming_chunk_preempts_without_archiving_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_upcoming")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-af_heart-say.txt"
    selected = engine.QUEUE / "003-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put("banked piece")
    state = engine.State()
    state.playing = current.name
    state.claimed.update({current.name, selected.name})

    request_id = engine.request_play(selected.stem)
    assert engine.process_play_request(buffered, state) == "select"
    acceptance = engine.wait_for_play_ack(request_id, timeout=0.1)

    assert current.is_file()
    assert not (engine.SPOKEN / current.name).exists()
    assert state.claimed == set()
    assert buffered.empty()

    assert engine.finish_chunk_playback(current, "select", False, state)
    assert state.playing is None
    assert current.is_file()
    assert not (engine.SPOKEN / current.name).exists()
    assert engine.claim_next_queued_chunk(state) == selected
    assert engine.claim_next_queued_chunk(state) == current
    assert acceptance["id"] == selected.stem


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
    assert archived.read_text(encoding="utf-8") == "Say this again"
    assert replay.read_text(encoding="utf-8") == "Say this again"
    assert engine.claim_next_queued_chunk(state) == replay
    assert engine.wait_for_play_ack(request_id, timeout=0.1)["id"] == archived.stem
    assert engine.archive(replay)
    assert [path.name for path in engine.SPOKEN.glob("*.txt")] == [archived.name]


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

    assert state.playing is None
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
    assert engine.claim_next_queued_chunk(state) is None


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


def test_startup_cleanup_removes_stale_play_request(tmp_path: Path) -> None:
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
    assert not request.exists()
    assert not claim.exists()
    assert not temporary.exists()
    assert not acknowledgement.exists()
    assert not queue_request.exists()
    assert not queue_acknowledgement.exists()


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
