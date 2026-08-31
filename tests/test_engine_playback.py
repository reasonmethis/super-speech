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
    committed_result,
    configure_runtime,
    load_engine,
    prepare_timeline,
    ready_status,
    rejected_result,
    request_mutation,
    set_current,
    speechicle_id,
)

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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    engine.PAUSE.touch()
    engine.STOP.touch()
    buffered: queue.Queue = queue.Queue()
    buffered.put("banked piece")
    state = engine.State()
    set_current(engine, state, current)
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


@pytest.mark.parametrize("pause_remains", [False, True])
def test_play_and_pause_apply_in_publication_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pause_remains: bool,
) -> None:
    engine = load_engine(f"super_speech_engine_ordered_pause_{pause_remains}")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    if pause_remains:
        request_id = request_mutation(
            engine, "play", id=speechicle_id(engine, current), voice=None
        )
        engine.publish_ordered_marker(engine.PAUSE)
    else:
        engine.publish_ordered_marker(engine.PAUSE)
        request_id = request_mutation(
            engine, "play", id=speechicle_id(engine, current), voice=None
        )
    request_path = next(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))
    os.utime(request_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(engine.PAUSE, ns=(1_000_000_000, 1_000_000_000))

    assert engine.process_mutation_requests(queue.Queue(), state) is None

    result = committed_result(engine, request_id)
    assert engine.PAUSE.exists() is pause_remains
    assert result["snapshot"]["state"] == ("paused" if pause_remains else "playing")


def test_pause_published_while_play_commits_is_not_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_pause_during_play")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, current), voice=None
    )
    original_apply = engine.apply_play_mutation

    def apply_then_pause(buffer, current_state, request):
        result = original_apply(buffer, current_state, request)
        engine.publish_ordered_marker(engine.PAUSE)
        return result

    monkeypatch.setattr(engine, "apply_play_mutation", apply_then_pause)

    engine.process_mutation_requests(queue.Queue(), state)

    committed_result(engine, request_id)
    assert engine.PAUSE.exists()


def test_legacy_pause_is_preserved_by_legacy_play_and_replaced_by_new_play(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_legacy_pause_upgrade")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    public_id = speechicle_id(engine, current)
    engine.PAUSE.touch()
    legacy_request_id = "a" * 24
    (engine.BASE / f"MUTATION.1.{legacy_request_id}.json").write_text(
        json.dumps(
            {
                "request_id": legacy_request_id,
                "type": "play",
                "id": public_id,
            }
        ),
        encoding="utf-8",
    )

    engine.process_mutation_requests(queue.Queue(), state)

    committed_result(engine, legacy_request_id)
    assert engine.PAUSE.exists()

    request_id = request_mutation(engine, "play", id=public_id, voice=None)
    engine.process_mutation_requests(queue.Queue(), state)

    committed_result(engine, request_id)
    assert not engine.PAUSE.exists()


def test_playing_queue_first_resumes_when_cached_projection_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_current_without_projection")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    engine.PAUSE.touch()
    state = engine.State()

    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, current), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) is None

    assert committed_result(engine, request_id)["result_id"] == speechicle_id(
        engine, current
    )
    assert not engine.PAUSE.exists()
    assert engine.queue_files_in_order() == [current, waiting]
    assert not list(engine.SPOKEN.glob("*.txt"))


def test_selecting_waiting_speechicle_archives_everything_older(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_upcoming")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    older = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    selected = engine.QUEUE / "003-sp_00000000000000000000000000000003-bm_fable-say.txt"
    newer = engine.QUEUE / "004-sp_00000000000000000000000000000004-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    older.write_text("Older waiting", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    newer.write_text("Newer waiting", encoding="utf-8")
    buffered: queue.Queue = queue.Queue()
    buffered.put("banked piece")
    state = engine.State()
    set_current(engine, state, current)
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
    assert state.current_projection is not None and state.current_projection.filename == selected.name
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
    older = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    newer = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (older, selected, newer):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()

    request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"

    assert not older.exists()
    assert (engine.SPOKEN / older.name).exists()
    assert engine.queue_files_in_order() == [selected, newer]
    assert state.current_projection is not None and state.current_projection.skip_initial_gap
    assert engine.consume_initial_gap_skip(state, selected.name)
    assert not engine.consume_initial_gap_skip(state, selected.name)
    assert state.current_projection is not None and not state.current_projection.skip_initial_gap


def test_selecting_waiting_chunk_rejects_an_archive_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_archive_failure")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    older = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    older.write_text("Older", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    monkeypatch.setattr(engine.timeline, "archive_many", lambda _paths: False)
    state = engine.State()

    request_id = request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) is None

    result = rejected_result(engine, request_id)
    assert "could not select" in str(result["error"])
    assert older.exists()
    assert selected.exists()


def test_selection_rolls_back_earlier_archives_when_a_later_archive_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_archive_rollback")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    older = [
        engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt",
        engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt",
    ]
    selected = engine.QUEUE / "003-sp_00000000000000000000000000000003-bm_fable-say.txt"
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
        engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt",
        engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt",
    ]
    selected = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
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
    archived = engine.SPOKEN / "007-sp_00000000000000000000000000000007-bm_fable-g350-say.txt"
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
        engine.SPOKEN / f"{number:03d}-sp_{number:032x}-af_heart-say.txt"
        for number in range(5, 0, -1)
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_history_order(history)
    prepare_timeline(engine)
    active: list[Path] = []
    if include_active:
        active = [
            engine.enqueue_text("006-af_heart-say", "af_heart"),
            engine.enqueue_text("007-af_heart-say", "af_heart"),
        ]
        engine.timeline.save_queue_order(active)

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
    assert engine.queue_files_in_order() == [
        *[engine.QUEUE / path.name for path in reversed(history[: history_index + 1])],
        *active,
    ]
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
    archived = engine.SPOKEN / "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    first = engine.QUEUE / "010-sp_0000000000000000000000000000000a-af_heart-say.txt"
    second = engine.QUEUE / "011-sp_0000000000000000000000000000000b-af_heart-say.txt"
    for path in (archived, first, second):
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_queue_order([first, second])

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
    for index, path in enumerate(history, start=1):
        path.write_text(f"History {index}", encoding="utf-8")
    engine.timeline.paths.queue_order.write_text(
        json.dumps({"version": 1, "ids": []}), encoding="utf-8"
    )
    engine.timeline.paths.history_order.write_text(
        json.dumps({"version": 1, "ids": [path.stem for path in history]}),
        encoding="utf-8",
    )
    os.replace(history[0], engine.QUEUE / history[0].name)
    os.replace(history[1], engine.QUEUE / history[1].name)

    prepare_timeline(engine)

    assert [path.read_text(encoding="utf-8") for path in engine.queue_files_in_order()] == [
        "History 2",
        "History 1",
    ]
    assert [
        path.read_text(encoding="utf-8") for path in engine.timeline.history_files()
    ] == ["History 3"]
    assert all(
        engine.SpeechicleFilename.parse(path.name)
        for path in [*engine.QUEUE.glob("*.txt"), *engine.SPOKEN.glob("*.txt")]
    )


def test_startup_removes_legacy_active_history_duplicates(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_legacy_duplicate_repair")
    configure_runtime(engine, tmp_path)
    active = engine.QUEUE / "002-af_heart-say.txt"
    duplicate = engine.SPOKEN / active.name
    earlier = engine.SPOKEN / "001-af_heart-say.txt"
    active.write_text("Active", encoding="utf-8")
    duplicate.write_text("Active", encoding="utf-8")
    earlier.write_text("Earlier", encoding="utf-8")
    engine.timeline.paths.queue_order.write_text(
        json.dumps({"version": 1, "ids": [active.stem]}), encoding="utf-8"
    )
    engine.timeline.paths.history_order.write_text(
        json.dumps({"version": 1, "ids": [duplicate.stem, earlier.stem]}),
        encoding="utf-8",
    )

    prepare_timeline(engine)

    assert [path.read_text(encoding="utf-8") for path in engine.queue_files_in_order()] == [
        "Active"
    ]
    assert [
        path.read_text(encoding="utf-8") for path in engine.timeline.history_files()
    ] == ["Earlier"]


def test_startup_promotes_rows_above_a_legacy_history_replay(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_legacy_replay_boundary")
    configure_runtime(engine, tmp_path)
    newest = engine.SPOKEN / "003-af_heart-say.txt"
    middle = engine.SPOKEN / "002-af_heart-say.txt"
    selected_history = engine.SPOKEN / "001-af_heart-say.txt"
    selected_queue = engine.QUEUE / selected_history.name
    for path, text in (
        (newest, "Newest"),
        (middle, "Middle"),
        (selected_history, "Selected"),
        (selected_queue, "Selected"),
    ):
        path.write_text(text, encoding="utf-8")
    engine.timeline.paths.queue_order.write_text(
        json.dumps({"version": 1, "ids": [selected_queue.stem]}),
        encoding="utf-8",
    )
    engine.timeline.paths.history_order.write_text(
        json.dumps(
            {
                "version": 1,
                "ids": [newest.stem, middle.stem, selected_history.stem],
            }
        ),
        encoding="utf-8",
    )

    prepare_timeline(engine)

    assert [path.read_text(encoding="utf-8") for path in engine.queue_files_in_order()] == [
        "Selected",
        "Middle",
        "Newest",
    ]
    assert engine.timeline.history_files() == []


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
    engine.timeline._write_order(engine.timeline.paths.queue_order, [duplicate_queue.stem], 1)
    engine.timeline._write_order(
        engine.timeline.paths.history_order, [duplicate_history.stem, selected.stem], 1
    )
    os.replace(duplicate_history, backup)
    engine.timeline._write_intent(
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

    prepare_timeline(engine)

    assert not backup.exists()
    assert [path.read_text(encoding="utf-8") for path in engine.queue_files_in_order()] == [
        "Selected",
        "Active copy",
    ]
    assert all(
        engine.SpeechicleFilename.parse(path.name)
        for path in engine.QUEUE.glob("*.txt")
    )
    assert engine.timeline.history_files() == []
    assert not engine.timeline.paths.intent.exists()


def test_preparation_rejects_a_nonidentical_canonical_identity_collision(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_promotion_deferred_duplicate")
    configure_runtime(engine, tmp_path)
    duplicate_history = engine.SPOKEN / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    duplicate_queue = engine.QUEUE / duplicate_history.name
    selected = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    duplicate_history.write_text("Legacy History copy", encoding="utf-8")
    duplicate_queue.write_text("Active copy", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    with pytest.raises(RuntimeError, match="target identities are not unique"):
        prepare_timeline(engine)

    assert duplicate_history.read_text(encoding="utf-8") == "Legacy History copy"
    assert duplicate_queue.read_text(encoding="utf-8") == "Active copy"
    assert selected.read_text(encoding="utf-8") == "Selected"


def test_enqueue_waits_while_history_rows_are_moving(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_timeline_file_lock")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    lock = engine.InterprocessFileLock(engine.timeline.paths.mutation_lock)
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
        engine.SPOKEN / "003-sp_00000000000000000000000000000003-af_heart-say.txt",
        engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt",
        engine.SPOKEN / "001-sp_00000000000000000000000000000001-bm_fable-say.txt",
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_history_order(history)
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

    with pytest.raises(PermissionError, match="locked"):
        engine.timeline.promote_history(history[-1])

    assert engine.timeline.history_files() == history
    assert not list(engine.QUEUE.glob("*.txt"))


def test_history_selection_excludes_worker_claims_until_rollback_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_boundary_worker_lock")
    configure_runtime(engine, tmp_path)
    source = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    source.write_text("History", encoding="utf-8")
    entered_save = threading.Event()
    release_save = threading.Event()
    real_write = engine.timeline._write_order

    def fail_after_worker_waits(path: Path, ids: list[str]) -> None:
        if path == engine.timeline.paths.queue_order and not entered_save.is_set():
            entered_save.set()
            assert release_save.wait(1)
            raise PermissionError("locked")
        real_write(path, ids)

    monkeypatch.setattr(engine.timeline, "_write_order", fail_after_worker_waits)
    promotion_errors = []

    def promote() -> None:
        try:
            engine.timeline.promote_history(source)
        except OSError as error:
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

    assert promotion_errors
    assert claimed == [None]
    assert state.claims == {}
    assert source.exists()


def test_history_selection_rejects_duplicate_id_across_sections(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_history_legacy_duplicate")
    configure_runtime(engine, tmp_path)
    duplicate_history = engine.SPOKEN / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    duplicate_queue = engine.QUEUE / duplicate_history.name
    selected = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    duplicate_history.write_text("Legacy replay", encoding="utf-8")
    duplicate_queue.write_text("Active copy", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    engine.timeline.save_history_order([duplicate_history, selected])
    with pytest.raises(RuntimeError, match="same ID in both sections"):
        engine.timeline.promote_history(selected)

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
    first = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    selected = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    engine.timeline.save_history_order([first, selected])
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
    archived = engine.SPOKEN / "007-sp_00000000000000000000000000000007-bm_fable-g350-say.txt"
    archived.write_text("Say this another way", encoding="utf-8")
    state = engine.State()

    original_id = speechicle_id(engine, archived)
    request_id = request_mutation(
        engine, "play", id=original_id, voice="af_heart"
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    result = committed_result(engine, request_id)
    variant = engine.timeline.find(engine.QUEUE, str(result["result_id"]))

    assert result["result_id"] == original_id
    assert variant is not None
    assert not archived.exists()
    assert variant.name == "007-sp_00000000000000000000000000000007-af_heart-g350-say.txt"
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
        engine.SPOKEN / "003-sp_00000000000000000000000000000003-af_heart-say.txt",
        engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt",
        engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt",
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_history_order(history)
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
        engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt",
        engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt",
    ]
    for path in history:
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_history_order(history)

    request_id = request_mutation(
        engine,
        "play",
        id=speechicle_id(engine, history[-1]),
        voice="bm_fable",
    )
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None

    result = rejected_result(engine, request_id)
    assert result["error"] == "unknown Kokoro voice: bm_fable"
    assert engine.timeline.history_files() == history
    assert not list(engine.QUEUE.glob("*.txt"))


def test_changing_a_waiting_voice_keeps_the_selection_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_waiting_voice")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    older = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-g500-say.txt"
    newer = engine.QUEUE / "003-sp_00000000000000000000000000000003-bm_fable-say.txt"
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
    variant = engine.timeline.find(engine.QUEUE, str(result["result_id"]))

    assert result["result_id"] == original_id
    assert variant is not None
    assert variant.name == "002-sp_00000000000000000000000000000002-af_heart-g500-say.txt"
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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-g600-say.txt"
    current.write_text("Current words", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    engine._record_claim(state, current)

    original_id = speechicle_id(engine, current)
    request_id = request_mutation(
        engine, "play", id=original_id, voice="bm_fable"
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    result = committed_result(engine, request_id)
    variant = engine.timeline.find(engine.QUEUE, str(result["result_id"]))

    assert result["result_id"] == original_id
    assert variant is not None
    assert variant.name == "001-sp_00000000000000000000000000000001-bm_fable-g600-say.txt"
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

    queued = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    archived = engine.SPOKEN / "007-sp_00000000000000000000000000000007-bm_fable-say.txt"
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
    earlier = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    earlier.write_text("Earlier", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()
    engine._record_claim(state, earlier)

    request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )
    assert engine.process_mutation_requests(queue.Queue(), state) == "select"

    assert state.current_projection is not None and state.current_projection.filename == selected.name
    assert state.claims == {}
    assert engine.claim_next_queued_chunk(state) == selected


def test_selecting_a_prefetched_item_restarts_synthesis_at_piece_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_play_prefetched_piece")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    monkeypatch.setattr(engine, "SPLIT_CHARS", 16)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    selected.write_text("First sentence. Second sentence.", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()

    request_mutation(
        engine, "play", id=speechicle_id(engine, selected), voice=None
    )

    assert engine.gap_wait(1.0, queue.Queue(), state) == "select"
    assert engine.claim_next_queued_chunk(state) == selected


def test_reordering_during_a_gap_preserves_the_held_current_piece(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_gap")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    held = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    selected = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    held.write_text("Held", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    state = engine.State()
    _, generation = engine._record_claim(state, held)
    request_id = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, selected),
        before_id=speechicle_id(engine, waiting),
    )

    assert engine.gap_wait(0.01, queue.Queue(), state) is None
    committed_result(engine, request_id)

    assert held.is_file()
    assert state.claims == {held.name: generation}
    assert not engine.buffered_piece_is_stale(state, held.name, generation)
    assert engine.queue_files_in_order() == [held, selected, waiting]


def test_queue_mutation_preserves_the_current_piece_held_before_playback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_held_piece_generation")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    moved = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (current, waiting, moved):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
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
    assert state.claims == {held_path.name: old_generation}
    assert not engine.buffered_piece_is_stale(
        state, held_path.name, old_generation
    )


def test_stale_worker_release_preserves_a_replacement_current_claim(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_stale_worker_claim_release")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    _, old_generation = engine._record_claim(state, current)
    engine.invalidate_claim(state, current.name)
    _, replacement_generation = engine._record_claim(state, current)

    engine.release_preplay_chunk(state, current.name, old_generation)

    assert state.claims == {current.name: replacement_generation}
    assert state.current_projection is not None and state.current_projection.filename == current.name


def test_piece_transition_for_another_filename_is_ignored(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_foreign_piece_progress")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    other = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    other.write_text("Other", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    original = state.current_projection

    assert not engine.update_current_piece(state, other.name, 1, 0, 5)
    assert state.current_projection is original


def test_stale_buffered_completion_cannot_clear_replacement_current(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_stale_current_completion")
    configure_runtime(engine, tmp_path)
    stale = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    replacement = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    stale.write_text("Stale", encoding="utf-8")
    replacement.write_text("Replacement", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, replacement)
    replacement_projection = state.current_projection

    assert engine.finish_chunk_playback(stale, "select", True, state)
    assert state.current_projection is replacement_projection


def test_queue_mutation_keeps_an_active_playback_claim_without_buffered_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_active_piece_generation")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    moved = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (current, waiting, moved):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
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


def test_queue_mutation_preserves_the_current_piece_held_during_a_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_queue_gap_generation")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    moved = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (current, waiting, moved):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
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

    assert engine.gap_wait(0.01, queue.Queue(), state) is None

    assert state.claims == {held_path.name: old_generation}
    assert not engine.buffered_piece_is_stale(
        state, held_path.name, old_generation
    )


@pytest.mark.parametrize("outcome", ["done", "skip"])
def test_failed_archive_keeps_chunk_claimed_instead_of_repeating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    engine = load_engine("super_speech_engine_archive_failure")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    engine._record_claim(state, current)
    monkeypatch.setattr(engine, "archive", lambda path: False)
    monkeypatch.setattr(engine, "archive_failed", lambda path: False)

    assert engine.finish_chunk_playback(current, outcome, True, state)

    assert state.current_projection is None
    assert set(state.claims) == {current.name}
    assert state.stop.is_set()
    assert engine.claim_next_queued_chunk(state) is None


def test_failed_clear_archive_stops_instead_of_leaving_a_stuck_waiting_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_clear_archive_failure")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    first_waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    locked_waiting = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (current, first_waiting, locked_waiting):
        path.write_text(path.stem, encoding="utf-8")
    ordered = [current, first_waiting, locked_waiting]
    engine.timeline.save_queue_order(ordered)
    state = engine.State()
    set_current(engine, state, current)
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
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    first_waiting = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    second_waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    for path in (current, first_waiting, second_waiting):
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_queue_order([current, first_waiting, second_waiting])
    state = engine.State()
    set_current(engine, state, current)

    engine.do_clear(queue.Queue(), state)

    assert not current.exists()
    assert engine.timeline.history_files() == [
        engine.SPOKEN / second_waiting.name,
        engine.SPOKEN / first_waiting.name,
        engine.SPOKEN / current.name,
    ]


def test_clear_does_not_manufacture_a_claim_for_a_preplay_current(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_preplay_claim")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)

    engine.do_clear(queue.Queue(), state)

    assert state.claims == {}
    assert state.current_projection is None
    assert engine.claim_next_queued_chunk(state) is None
    assert not current.exists()
    assert not waiting.exists()


def test_failed_synthesis_archive_stops_instead_of_leaving_a_stuck_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_synth_archive_failure")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
    old = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    old.write_text("Old", encoding="utf-8")
    selected.write_text("Selected", encoding="utf-8")
    entered_synthesis = threading.Event()
    release_synthesis = threading.Event()
    selected_synthesis = threading.Event()
    finish_test = threading.Event()
    calls = 0
    state = engine.State()
    set_current(engine, state, old)

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
        engine.replace_current_projection(
            state,
            selected.name,
            selected.read_text(encoding="utf-8"),
            engine.voice_from_name(selected.name),
        )
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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
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
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting.write_text("" if failure == "empty" else "Waiting", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, waiting)

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
    assert state.current_projection is None
    assert state.claims == {}


def test_transient_current_read_failure_remains_claimable_during_stop(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_current_read_failure")
    configure_runtime(engine, tmp_path)
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
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
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, waiting)
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
    assert state.current_projection is None
    assert waiting.exists()


def test_stop_during_a_gap_keeps_the_next_chunk_queued(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_stop_gap")
    configure_runtime(engine, tmp_path)
    next_chunk = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
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


@pytest.mark.parametrize("expected", ["stop", "clear"])
def test_clear_and_stop_follow_publication_order_during_a_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    expected: str,
) -> None:
    engine = load_engine(f"super_speech_engine_clear_stop_order_{expected}")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.SIGNAL_TICK = 0.001
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    if expected == "stop":
        request_id = request_mutation(engine, "clear")
        engine.publish_ordered_marker(engine.STOP)
    else:
        engine.publish_ordered_marker(engine.STOP)
        request_id = request_mutation(engine, "clear")
    request = next(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))
    os.utime(request, ns=(1_000_000_000, 1_000_000_000))
    os.utime(engine.STOP, ns=(1_000_000_000, 1_000_000_000))

    assert engine.gap_wait(0.01, queue.Queue(), state) == expected
    if expected == "stop":
        assert current.exists()
        assert not (engine.SPOKEN / current.name).exists()
    else:
        assert not current.exists()
        assert (engine.SPOKEN / current.name).exists()
        assert state.current_projection is None


def test_new_speech_cancels_a_pending_graceful_stop(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_continue_gap")
    configure_runtime(engine, tmp_path)
    engine.SIGNAL_TICK = 0.001
    state = engine.State()
    state.saw_stop = True
    engine.publish_ordered_marker(engine.STOP)
    engine.publish_ordered_marker(engine.CONTINUE)

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
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    state.saw_stop = True
    if play_is_newer:
        engine.publish_ordered_marker(engine.STOP)
        request_id = request_mutation(
            engine, "play", id=speechicle_id(engine, current), voice=None
        )
    else:
        request_id = request_mutation(
            engine, "play", id=speechicle_id(engine, current), voice=None
        )
        engine.publish_ordered_marker(engine.STOP)
    request = next(engine.BASE.glob(f"MUTATION.*.{request_id}.json"))
    os.utime(request, ns=(1_000_000_000, 1_000_000_000))
    os.utime(engine.STOP, ns=(1_000_000_000, 1_000_000_000))

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


def test_stop_published_while_mutation_commits_survives_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_stop_during_mutation")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    history = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    history.write_text("Delete", encoding="utf-8")
    request_id = request_mutation(
        engine, "delete", id=speechicle_id(engine, history)
    )
    original_apply = engine.apply_delete_mutation

    def apply_then_stop(request) -> None:
        original_apply(request)
        engine.publish_ordered_marker(engine.STOP)

    monkeypatch.setattr(engine, "apply_delete_mutation", apply_then_stop)

    engine.process_mutation_requests(queue.Queue(), engine.State())

    committed_result(engine, request_id)
    assert not history.exists()
    assert engine.STOP.exists()


def test_a_newer_stop_is_not_canceled_by_an_older_new_work_notice(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_newer_stop_wins")
    configure_runtime(engine, tmp_path)
    engine.SIGNAL_TICK = 0.001
    state = engine.State()
    engine.publish_ordered_marker(engine.CONTINUE)
    engine.publish_ordered_marker(engine.STOP)
    os.utime(engine.CONTINUE, ns=(1_000_000_000, 1_000_000_000))
    os.utime(engine.STOP, ns=(1_000_000_000, 1_000_000_000))

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
    queued: list[tuple[str, str, int | None, str | None, str | None]] = []
    monkeypatch.setattr(engine, "start_engine", lambda: starts.append("start"))

    def enqueue(
        text: str,
        voice: str,
        gap: int | None,
        source: str | None,
        inbox: str | None,
    ) -> Path:
        queued.append((text, voice, gap, source, inbox))
        return (
            engine.QUEUE
            / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
        )

    monkeypatch.setattr(engine, "enqueue_text", enqueue)
    monkeypatch.setattr(
        engine, "public_id_for_path", lambda _path: f"sp_{'1' * 32}"
    )
    monkeypatch.setattr(
        engine,
        "wait_for_queue_acceptance",
        lambda: starts.append("accept") or True,
    )

    inbox = tmp_path / "agent-inbox.jsonl"
    assert engine.cli(
        [
            "speak",
            "New work",
            "--source",
            "Codex UI task",
            "--inbox",
            str(inbox),
        ]
    ) == 0
    assert starts == ["start", "accept"]
    assert queued == [
        ("New work", "af_heart", None, "Codex UI task", str(inbox))
    ]
    assert engine.CONTINUE.exists()


def test_speak_reports_durable_queueing_when_engine_cannot_accept_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = load_engine("super_speech_engine_speak_queued_later")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    monkeypatch.setattr(engine, "start_engine", lambda: None)
    monkeypatch.setattr(engine, "wait_for_queue_acceptance", lambda: False)

    assert engine.cli(["speak", "New work"]) == 0

    captured = capsys.readouterr()
    assert "speech remains queued; playback will begin when the engine is ready" in captured.err
    queued = engine.timeline.find(engine.QUEUE, captured.out.strip())
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
    waiting = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
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
    next_chunk = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    next_chunk.write_text("Next", encoding="utf-8")
    state = engine.State()
    engine._record_claim(state, next_chunk)
    request_mutation(engine, "clear")

    assert engine.gap_wait(1.0, queue.Queue(), state) == "clear"
    assert not next_chunk.exists()
    assert (engine.SPOKEN / next_chunk.name).read_text(encoding="utf-8") == "Next"


def test_clear_silences_playback_before_archiving_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_clear_silences_first")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.SIGNAL_TICK = 0.001
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    stream_started = threading.Event()
    clear_started = threading.Event()
    release_clear = threading.Event()
    playbacks = []
    outcomes: list[str] = []

    class FakeOutputStream:
        def __init__(self, *, callback, **_kwargs) -> None:
            self.playback = callback.__self__
            self.active = False
            playbacks.append(self.playback)

        def start(self) -> None:
            self.active = True
            stream_started.set()

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            self.active = False

    original_clear = engine.do_clear

    def slow_clear(buffer, current_state) -> bool:
        clear_started.set()
        release_clear.wait()
        return original_clear(buffer, current_state)

    monkeypatch.setattr(engine, "do_clear", slow_clear)
    sounddevice = SimpleNamespace(
        CallbackStop=CallbackStop,
        OutputStream=FakeOutputStream,
    )

    def run_playback() -> None:
        outcomes.append(
            engine.play_one(
                sounddevice,
                np,
                current,
                np.ones(10_000, dtype=np.float32),
                1_000,
                "chunk",
                queue.Queue(),
                state,
            )
        )

    playback_thread = threading.Thread(target=run_playback)
    playback_thread.start()
    try:
        assert stream_started.wait(1)
        request_id = request_mutation(engine, "clear")
        assert clear_started.wait(1)
        playback = playbacks[0]
        position = playback.position
        output = np.empty((4, 1), dtype=np.float32)
        playback.callback(output, 4, None, None)
        np.testing.assert_array_equal(output[:, 0], [0, 0, 0, 0])
        assert playback.position == position
    finally:
        release_clear.set()
        playback_thread.join(2)

    assert not playback_thread.is_alive()
    assert outcomes == ["clear"]
    committed_result(engine, request_id)


def test_clear_cannot_race_a_worker_refilling_a_full_buffer(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_full_buffer")
    configure_runtime(engine, tmp_path)
    engine.SIGNAL_TICK = 0.001
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    queued = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    queued.write_text("Queued", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
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
