from __future__ import annotations

import importlib.util
import json
import queue
import sys
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
    engine.PLAY = tmp_path / "PLAY.json"
    engine.STATUS = tmp_path / "status.json"
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
    assert status["version"] == 2
    assert status["history_count"] == 0
    assert status["history"] == []


def test_enqueue_text_reserves_the_next_queue_number(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_enqueue")

    configure_runtime(engine, tmp_path)
    (engine.SPOKEN / "007-af_heart-say.txt").write_text("Earlier", encoding="utf-8")

    queued = engine.enqueue_text("New words", "bm_fable", 650)

    assert queued.name == "008-bm_fable-g650-say.txt"
    assert queued.read_text(encoding="utf-8") == "New words"


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
        json.dumps({"engine_pid": fake_pid, "updated_at": 0}), encoding="utf-8"
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
            json.dumps({"engine_pid": fake_pid, "updated_at": engine.time.time() + 1}),
            encoding="utf-8",
        )

    monkeypatch.setattr(engine, "engine_is_running", engine_running)
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(engine.time, "sleep", publish_ready)

    engine.start_engine()

    assert sleeps == 1


def test_pause_and_resume_commands_share_the_runtime_signal(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_pause")
    configure_runtime(engine, tmp_path)

    assert engine.cli(["pause"]) == 0
    assert engine.PAUSE.is_file()
    assert engine.cli(["resume"]) == 0
    assert not engine.PAUSE.exists()


def test_play_payload_is_last_write_wins_without_unlinking_a_new_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_signal")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)

    engine.request_play("001-af_heart-say")
    claimed = engine.claim_play_request()
    assert claimed is not None
    engine.request_play("002-bm_fable-say")

    first = json.loads(claimed.read_text(encoding="utf-8"))
    claimed.unlink()
    assert first == {"id": "001-af_heart-say"}
    assert engine.take_play_request() == "002-bm_fable-say"
    assert engine.take_play_request() is None


def test_play_command_starts_engine_then_publishes_the_requested_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_cli")
    configure_runtime(engine, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(engine, "start_engine", lambda: calls.append("start"))
    monkeypatch.setattr(engine, "request_play", lambda chunk_id: calls.append(chunk_id))

    assert engine.cli(["play", "007-bm_fable-say"]) == 0
    assert calls == ["start", "007-bm_fable-say"]


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

    engine.request_play(current.stem)
    assert engine.process_play_request(buffered, state) is None

    assert not engine.PAUSE.exists()
    assert not engine.STOP.exists()
    assert not state.saw_stop
    assert state.claimed == {current.name}
    assert buffered.get_nowait() == "banked piece"


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

    engine.request_play(selected.stem)
    assert engine.process_play_request(buffered, state) == "select"

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


def test_replaying_history_copies_it_to_a_new_selected_queue_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_history")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    archived = engine.SPOKEN / "007-bm_fable-g350-say.txt"
    archived.write_text("Say this again", encoding="utf-8")
    state = engine.State()

    engine.request_play(archived.stem)
    assert engine.process_play_request(queue.Queue(), state) == "select"

    replay = engine.QUEUE / "008-bm_fable-g350-say.txt"
    assert archived.read_text(encoding="utf-8") == "Say this again"
    assert replay.read_text(encoding="utf-8") == "Say this again"
    assert engine.claim_next_queued_chunk(state) == replay


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
    engine.PLAY.write_text(json.dumps({"id": archived.stem}), encoding="utf-8")

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
            if len(played_samples) == 1:
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

    replay = engine.SPOKEN / "008-bm_fable-say.txt"
    assert played_samples == [8, 1]
    assert archived.read_text(encoding="utf-8") == "Replay me"
    assert replay.read_text(encoding="utf-8") == "Replay me"
    assert (engine.SPOKEN / queued.name).read_text(encoding="utf-8") == "First queued"
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


def test_failed_archive_keeps_chunk_claimed_instead_of_repeating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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

    assert engine.finish_chunk_playback(current, "done", True, state)

    assert state.playing is None
    assert state.claimed == {current.name}
    assert engine.claim_next_queued_chunk(state) is None


def test_startup_cleanup_removes_stale_play_request(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_play")
    configure_runtime(engine, tmp_path)
    engine.PLAY.write_text('{"id":"001-af_heart-say"}', encoding="utf-8")

    engine.clear_transient_signals()

    assert not engine.PLAY.exists()


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

    assert status["version"] == 2
    assert status["history_count"] == 3
    assert [item["text"] for item in status["history"]] == ["History 3", "History 2"]
