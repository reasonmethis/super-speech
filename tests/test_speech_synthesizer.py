from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from engine_test_support import configure_runtime, load_engine, prepare_timeline
from speech_synthesizer import SpeechSynthesizer


def test_voice_families_share_the_sample_output_contract(monkeypatch):
    import sherpa_onnx

    kokoro = Mock()
    kokoro_samples = np.array([0.1, 0.2], dtype=np.float32)
    kokoro.create.return_value = (kokoro_samples, 24000)
    alba = Mock()
    alba.generate.return_value = SimpleNamespace(samples=[0.3, 0.4], sample_rate=22050)
    construct = Mock(return_value=alba)
    monkeypatch.setattr(sherpa_onnx, "OfflineTts", construct)
    synthesizer = SpeechSynthesizer(kokoro, Path("models"))
    samples, rate = synthesizer.create("Hello", voice="af_heart", speed=1, lang="en-us")
    assert samples is kokoro_samples
    assert rate == 24000
    construct.assert_not_called()
    for text in ("First", "Second"):
        samples, rate = synthesizer.create(text, voice="piper_alba", speed=1, lang="en-us")
        assert isinstance(samples, np.ndarray)
        assert samples.dtype == np.float32
        assert samples.ndim == 1
        np.testing.assert_allclose(samples, [0.3, 0.4])
        assert rate == 22050
    construct.assert_called_once()
    alba.generate.assert_called_with("Second", sid=0, speed=1)
    samples, rate = synthesizer.create("Again", voice="af_heart", speed=1, lang="en-us")
    assert samples is kokoro_samples
    assert rate == 24000


def test_alba_round_trips_through_storage_and_commands(tmp_path):
    engine = load_engine("alba_storage")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    item = engine.enqueue_text("Hello", "piper_alba")
    assert engine.voice_from_name(item.name) == "piper_alba"
    request = engine.parse_cli_mutation({"type": "play", "id": engine.public_id_for_path(item), "voice": "piper_alba"}, "a" * 24)
    assert request.voice == "piper_alba"


def test_history_request_grows_one_authoritative_view(tmp_path, monkeypatch):
    engine = load_engine("history_loading")
    configure_runtime(engine, tmp_path)
    for number in range(1, 106):
        (engine.SPOKEN / f"{number:03d}-sp_{number:032x}-af_heart-say.txt").write_text(str(number))
    state = engine.State()
    first = engine.publish_status(state, force=True)
    assert len(first["history"]) == 50
    monkeypatch.setattr(engine, "_read_authoritative_status", lambda: engine.publish_status(state, force=True))
    more = engine.execute_control_request({"command": "history", "limit": 100})
    assert len(more["history"]) == 100
    assert more["history"][:50] == first["history"]
    assert more["timeline_revision"] > first["timeline_revision"]
    assert engine.publish_status(state, force=True)["history"] == more["history"]
    last = engine.execute_control_request({"command": "history", "limit": 150})
    assert len(last["history"]) == last["history_count"] == 105
    assert len(engine.execute_control_request({"command": "history", "limit": 50})["history"]) == 105
    for limit in (True, 0, 49, "100", 100.5, 1_000_001):
        with pytest.raises(ValueError):
            engine.execute_control_request({"command": "history", "limit": limit})
