from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from engine_test_support import configure_runtime, load_engine


def status_snapshot(engine) -> dict[str, object]:
    return {
        "version": engine.STATUS_VERSION,
        "timeline_revision": 4,
        "state": "idle",
        "updated_at": 123.0,
        "engine_pid": 456,
        "current": None,
        "queue_count": 0,
        "queue": [],
        "history_count": 0,
        "history": [],
    }


def test_python_status_validator_matches_shared_protocol_corpus() -> None:
    engine = load_engine("super_speech_engine_shared_status_protocol")
    corpus = json.loads(
        (Path(__file__).parent / "fixtures" / "status_protocol.json").read_text(
            encoding="utf-8"
        )
    )

    assert all(engine._snapshot_is_valid(item) for item in corpus["valid"])
    assert not any(engine._snapshot_is_valid(item) for item in corpus["invalid"])


def test_python_status_validator_rejects_the_same_impossible_states_as_the_app() -> None:
    engine = load_engine("super_speech_engine_status_protocol_invariants")
    public_id = f"sp_{'1' * 32}"
    current = {
        "id": public_id,
        "text": "Speech",
        "voice": "af_heart",
        "piece": 1,
        "piece_count": 1,
        "piece_start": 0,
        "piece_end": len("Speech"),
        "elapsed_seconds": 0.0,
    }
    base = status_snapshot(engine)
    valid = {**base, "state": "playing", "current": current}

    assert engine._snapshot_is_valid(valid)
    for invalid in [
        {**base, "state": "paused"},
        {
            **base,
            "queue_count": 1,
            "queue": [{"id": public_id, "text": "Waiting", "voice": "af_heart"}],
        },
        {
            **valid,
            "current": {**current, "piece_end": len("Speech") + 1},
        },
        {
            **valid,
            "history_count": 1,
            "history": [
                {"id": public_id, "text": "Duplicate", "voice": "af_heart"}
            ],
        },
    ]:
        assert not engine._snapshot_is_valid(invalid)


def test_unreadable_current_has_one_empty_piece_status_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_unreadable_current_protocol")
    configure_runtime(engine, tmp_path)
    current_path = (
        engine.QUEUE
        / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    )
    current_path.write_text("Temporarily locked", encoding="utf-8")
    original_read_text = Path.read_text
    failures_remaining = 3

    def read_text(path: Path, *args, **kwargs) -> str:
        nonlocal failures_remaining
        if path == current_path and failures_remaining:
            failures_remaining -= 1
            raise PermissionError("temporarily locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    projected = engine.publish_status("playing", engine.State(), force=True)
    monkeypatch.setattr(engine, "activate_next_chunk", lambda _state: False)
    direct = engine.publish_status("playing", engine.State(), force=True)
    stopped = engine._stopped_status_payload()

    assert projected is not None
    assert direct is not None
    expected = {
        "id": "sp_00000000000000000000000000000001",
        "text": "",
        "voice": "af_heart",
        "piece": 0,
        "piece_count": 1,
        "piece_start": None,
        "piece_end": None,
        "elapsed_seconds": 0.0,
    }
    assert json.loads(json.dumps(projected["current"])) == expected
    assert json.loads(json.dumps(direct["current"])) == expected
    assert json.loads(json.dumps(stopped["current"])) == expected
    assert failures_remaining == 0


def test_stopped_status_includes_recent_history_ids(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_stopped_history_protocol")
    configure_runtime(engine, tmp_path)
    engine.HISTORY_LIMIT = 1
    older = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    newer = engine.SPOKEN / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    older.write_text("Older", encoding="utf-8")
    newer.write_text("Newer", encoding="utf-8")
    engine.timeline.save_history_order([newer, older])

    stopped = engine._stopped_status_payload()

    assert stopped["state"] == "stopped"
    assert stopped["history_count"] == 2
    assert stopped["history"] == [
        {
            "id": "sp_00000000000000000000000000000002",
            "text": "Newer",
            "voice": "bm_fable",
        }
    ]


def test_private_mutate_prints_unconfirmed_result_and_exits_successfully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = load_engine("super_speech_engine_unconfirmed_mutate_protocol")
    configure_runtime(engine, tmp_path)
    snapshot = status_snapshot(engine)
    engine.STATUS.write_text(json.dumps(snapshot), encoding="utf-8")
    request_ids: list[str] = []

    def request_mutation(request) -> str:
        request_ids.append(request.request_id)
        return request.request_id

    def wait_for_result(_request_id: str) -> dict[str, object]:
        raise engine.MutationOutcomeUnconfirmed(
            "mutation result was unconfirmed"
        )

    status_reads = 0

    def read_status() -> dict[str, object]:
        nonlocal status_reads
        status_reads += 1
        if status_reads == 1:
            return snapshot
        raise RuntimeError("status became unreadable")

    monkeypatch.setattr(engine, "start_engine", lambda: None)
    monkeypatch.setattr(engine, "_read_authoritative_status", read_status)
    monkeypatch.setattr(engine, "request_mutation", request_mutation)
    monkeypatch.setattr(engine, "wait_for_mutation_result", wait_for_result)

    exit_code = engine.cli(["mutate", json.dumps({"type": "clear"})])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {
        "outcome": "unconfirmed",
        "request_id": request_ids[0],
        "snapshot": snapshot,
        "error": "mutation result was unconfirmed",
    }


@pytest.mark.parametrize(
    ("outcome", "result_id", "error"),
    [
        ("committed", None, "unexpected error"),
        ("rejected", None, None),
        ("unconfirmed", f"sp_{'1' * 32}", "uncertain"),
    ],
)
def test_mutation_result_payload_rejects_impossible_field_combinations(
    outcome: str,
    result_id: str | None,
    error: str | None,
) -> None:
    engine = load_engine(f"super_speech_engine_invalid_{outcome}_result")

    with pytest.raises(ValueError):
        engine._mutation_result_payload(
            "1" * 24,
            outcome,
            status_snapshot(engine),
            result_id=result_id,
            error=error,
        )

def test_public_status_contains_no_storage_filenames(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_public_status_shape")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    archived = engine.SPOKEN / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
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
        (
            {
                "request_id": "e" * 24,
                "type": "clear",
                "command_sequence": 7,
            },
            "ClearMutation",
        ),
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
        {"request_id": "a" * 24, "type": "clear", "command_sequence": 0},
        {"request_id": "a" * 24, "type": "clear", "command_sequence": True},
    ],
)
def test_mutation_envelope_rejects_invalid_shapes(payload: object) -> None:
    engine = load_engine("super_speech_engine_invalid_envelope")

    with pytest.raises(ValueError):
        engine.parse_durable_mutation(payload)


@pytest.mark.parametrize(
    ("order_token", "payload_sequence"),
    [
        ("s00000000000000000002", 1),
        ("s00000000000000000002", None),
        ("1", 1),
    ],
)
def test_mutation_claim_rejects_filename_payload_sequence_mismatch(
    tmp_path: Path,
    order_token: str,
    payload_sequence: int | None,
) -> None:
    engine = load_engine("super_speech_engine_claim_sequence_mismatch")
    configure_runtime(engine, tmp_path)
    request_id = "a" * 24
    claim = engine.BASE / f"MUTATION.{order_token}.{request_id}.claim"
    payload: dict[str, object] = {
        "request_id": request_id,
        "type": "clear",
    }
    if payload_sequence is not None:
        payload["command_sequence"] = payload_sequence
    claim.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match its filename"):
        engine.read_mutation_claim(claim)


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
    monkeypatch.setattr(engine, "_read_authoritative_status", lambda: {})

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
