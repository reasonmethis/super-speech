from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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

    engine.QUEUE = tmp_path / "queue"
    engine.STATUS = tmp_path / "status.json"
    engine.PAUSE = tmp_path / "PAUSE"
    engine.QUEUE.mkdir()
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


def test_enqueue_text_reserves_the_next_queue_number(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_enqueue")

    engine.QUEUE = tmp_path / "queue"
    engine.SPOKEN = tmp_path / "spoken"
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    (engine.SPOKEN / "007-af_heart-say.txt").write_text("Earlier", encoding="utf-8")

    queued = engine.enqueue_text("New words", "bm_fable", 650)

    assert queued.name == "008-bm_fable-g650-say.txt"
    assert queued.read_text(encoding="utf-8") == "New words"


@pytest.mark.parametrize(
    ("voice", "gap_ms"),
    [("heart", None), ("af_heart", -1), ("af_heart", 1501)],
)
def test_enqueue_text_rejects_invalid_metadata(
    tmp_path: Path, voice: str, gap_ms: int | None
) -> None:
    engine = load_engine("super_speech_engine_invalid")
    engine.QUEUE = tmp_path / "queue"
    engine.SPOKEN = tmp_path / "spoken"

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


def test_pause_and_resume_commands_share_the_runtime_signal(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_pause")
    engine.BASE = tmp_path
    engine.PAUSE = tmp_path / "PAUSE"

    assert engine.cli(["pause"]) == 0
    assert engine.PAUSE.is_file()
    assert engine.cli(["resume"]) == 0
    assert not engine.PAUSE.exists()
