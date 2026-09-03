from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path

import pytest

from engine_test_support import (
    build_mutation,
    committed_result,
    configure_runtime,
    load_engine,
    prepare_timeline,
    rejected_result,
    request_mutation,
    set_current,
    speechicle_id,
    write_upgrade_catalog,
)
from speechicle_identity import IdentityCatalog, write_catalog
from timeline_storage import TimelineLocation, TimelineMove, TimelinePlan


def test_timeline_intent_accepts_replace_that_completed_before_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_intent_verified_replace")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    prepare_timeline(engine)
    real_replace = engine.os.replace

    def replace_then_report_error(source, destination) -> None:
        real_replace(source, destination)
        if Path(destination) == engine.timeline.paths.intent:
            raise PermissionError("replace reported an error after commit")

    monkeypatch.setattr(engine.os, "replace", replace_then_report_error)

    assert engine.archive(current)
    assert not current.exists()
    assert (engine.SPOKEN / current.name).is_file()


def test_voice_change_accepts_replace_that_completed_before_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_voice_verified_replace")
    configure_runtime(engine, tmp_path)
    source = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    source.write_text("Current", encoding="utf-8")
    prepare_timeline(engine)
    target = engine.timeline.voice_variant(source, "bm_fable")
    real_replace = engine.os.replace

    def replace_then_report_error(actual_source, destination) -> None:
        real_replace(actual_source, destination)
        if Path(actual_source) == source and Path(destination) == target:
            raise PermissionError("replace reported an error after commit")

    monkeypatch.setattr(engine.os, "replace", replace_then_report_error)

    assert engine.timeline.replace_queue_voice(source, "bm_fable") == target
    assert target.read_text(encoding="utf-8") == "Current"
    assert not source.exists()


def test_selection_preserves_one_visible_timeline_order(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_selection_visible_order")
    configure_runtime(engine, tmp_path)
    queue_paths = [
        engine.QUEUE / f"00{sequence}-sp_{sequence:032x}-af_heart-say.txt"
        for sequence in range(1, 4)
    ]
    history_paths = [
        engine.SPOKEN / f"00{sequence}-sp_{sequence:032x}-af_heart-say.txt"
        for sequence in range(6, 3, -1)
    ]
    for path in [*queue_paths, *history_paths]:
        path.write_text(path.stem, encoding="utf-8")
    prepare_timeline(engine)
    engine.timeline.save_queue_order(queue_paths)
    engine.timeline.save_history_order(history_paths)

    def visible_ids() -> list[str]:
        queue_ids = [
            speechicle_id(engine, path) for path in engine.timeline.queue_files()
        ]
        history_ids = [
            speechicle_id(engine, path) for path in engine.timeline.history_files()
        ]
        return [*reversed(queue_ids), *history_ids]

    original_order = visible_ids()
    waiting = engine.timeline.select(speechicle_id(engine, queue_paths[1]))
    current = engine.timeline.select(speechicle_id(engine, waiting.target))
    history = engine.timeline.select(speechicle_id(engine, history_paths[1]))

    assert waiting.origin == "waiting"
    assert waiting.moved_count == 1
    assert current.origin == "current"
    assert not current.restart_playback
    assert history.origin == "history"
    assert history.moved_count == 3
    assert visible_ids() == original_order


def test_history_reordering_is_persisted_within_the_recent_window(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_order")
    configure_runtime(engine, tmp_path)
    engine.HISTORY_LIMIT = 2
    first = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    third = engine.SPOKEN / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")

    engine.apply_history_move_mutation(
        build_mutation(
            engine,
            "move",
            section="history",
            id=speechicle_id(engine, third),
            before_id=None,
        )
    )

    assert engine.timeline.history_files() == [second, third, first]
    assert [item["id"] for item in engine.history_snapshot()[1]] == [
        speechicle_id(engine, second),
        speechicle_id(engine, third),
    ]


def test_new_history_item_stays_newest_after_manual_reordering(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_history_newest")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    engine.timeline.save_history_order([first, second])
    prepare_timeline(engine)
    newest = engine.enqueue_text("Newest", "af_heart")

    assert engine.archive(newest)

    assert engine.timeline.history_files() == [engine.SPOKEN / newest.name, first, second]


def test_new_archive_and_history_reorder_commit_in_one_serial_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_serial_order")
    configure_runtime(engine, tmp_path)
    first = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    newest = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (first, second, newest):
        path.write_text(path.stem, encoding="utf-8")
    engine.timeline.save_history_order([first, second])
    entered_save = threading.Event()
    release_save = threading.Event()
    real_save = engine.timeline.save_history_order

    def pause_reorder_save(paths=None) -> None:
        if threading.current_thread().name == "history-reorder":
            entered_save.set()
            assert release_save.wait(1)
        real_save(paths)

    monkeypatch.setattr(engine.timeline, "save_history_order", pause_reorder_save)
    second_id = speechicle_id(engine, second)
    first_id = speechicle_id(engine, first)
    reorder = threading.Thread(
        name="history-reorder",
        target=engine.apply_history_move_mutation,
        args=(
            build_mutation(
                engine,
                "move",
                section="history",
                id=second_id,
                before_id=first_id,
            ),
        ),
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
    assert engine.timeline.history_files() == [engine.SPOKEN / newest.name, second, first]


def test_history_snapshot_waits_for_an_archive_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_history_lock_order")
    configure_runtime(engine, tmp_path)
    archived = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    queued = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    archived.write_text("Earlier", encoding="utf-8")
    queued.write_text("New", encoding="utf-8")
    archive_holds_order = threading.Event()
    snapshot_started = threading.Event()
    allow_archive_write = threading.Event()
    real_write = engine.timeline._write_order

    def gated_write(path: Path, ids: list[str]) -> None:
        if path == engine.timeline.paths.history_order and threading.current_thread().name == "archive":
            archive_holds_order.set()
            assert allow_archive_write.wait(1)
        real_write(path, ids)

    monkeypatch.setattr(engine.timeline, "_write_order", gated_write)
    snapshots = []

    def take_snapshot() -> None:
        snapshot_started.set()
        snapshots.append(engine.history_snapshot())

    archive_thread = threading.Thread(
        name="archive",
        target=engine.archive,
        args=(queued,),
        daemon=True,
    )
    snapshot_thread = threading.Thread(
        name="snapshot",
        target=take_snapshot,
        daemon=True,
    )

    archive_thread.start()
    assert archive_holds_order.wait(1)
    snapshot_thread.start()
    try:
        assert snapshot_started.wait(1)
        assert snapshot_thread.is_alive()
    finally:
        allow_archive_write.set()
    archive_thread.join(1)
    snapshot_thread.join(1)

    assert not archive_thread.is_alive()
    assert not snapshot_thread.is_alive()
    snapshot_count, snapshot_items = snapshots[0]
    assert snapshot_count == 2
    assert [item["text"] for item in snapshot_items] == ["New", "Earlier"]


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

    prepare_timeline(engine)
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
    first = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    prepare_timeline(engine)

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
    first = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    prepare_timeline(engine)
    assert engine.history_snapshot()[1][0]["text"] == "First"
    queued_second = engine.enqueue_text("Second", "af_heart")
    second = engine.SPOKEN / queued_second.name
    original_read_text = Path.read_text

    def locked_text(path: Path, *args, **kwargs) -> str:
        if path in {first, second}:
            raise PermissionError("locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked_text)
    assert engine.archive(queued_second)
    count, items = engine.history_snapshot()

    assert count == len(items) == 2
    assert [item["id"] for item in items] == [
        speechicle_id(engine, second),
        speechicle_id(engine, first),
    ]
    assert items[0]["text"] == ""
    assert items[1]["text"] == "First"


def test_timeline_plan_executes_moves_orders_and_cleanup(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_timeline_plan_success")
    configure_runtime(engine, tmp_path)
    source = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    target = engine.SPOKEN / source.name
    source.write_text("Speech", encoding="utf-8")
    row_id = speechicle_id(engine, source)
    plan = TimelinePlan(
        kind="archive",
        moves=(
            TimelineMove(
                TimelineLocation("queue", source.name),
                TimelineLocation("history", target.name),
            ),
        ),
        previous_queue_ids=(row_id,),
        previous_history_ids=(),
        queue_ids=(),
        history_ids=(row_id,),
    )

    engine.timeline._execute_plan(plan)

    assert not source.exists()
    assert target.read_text(encoding="utf-8") == "Speech"
    assert engine.queue_files_in_order() == []
    assert engine.timeline.history_files() == [target]
    assert not engine.timeline.paths.intent.exists()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_timeline_plan_rolls_back_after_moves_and_one_order_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rollback_fails: bool,
) -> None:
    engine = load_engine(
        f"super_speech_engine_timeline_plan_rollback_{rollback_fails}"
    )
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    source = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    target = engine.QUEUE / source.name
    current.write_text("Current", encoding="utf-8")
    source.write_text("History", encoding="utf-8")
    current_id = speechicle_id(engine, current)
    source_id = speechicle_id(engine, source)
    engine.timeline.save_queue_order([current])
    engine.timeline.save_history_order([source])
    plan = TimelinePlan(
        kind="promote",
        moves=(
            TimelineMove(
                TimelineLocation("history", source.name),
                TimelineLocation("queue", target.name),
            ),
        ),
        previous_queue_ids=(current_id,),
        previous_history_ids=(source_id,),
        queue_ids=(source_id, current_id),
        history_ids=(),
    )
    real_write = engine.timeline._write_order
    real_replace = engine.os.replace

    def fail_final_history_order(path: Path, ids: list[str]) -> None:
        if path == engine.timeline.paths.history_order and ids == []:
            raise PermissionError("history order locked")
        real_write(path, ids)

    def maybe_fail_move_rollback(source_path, target_path) -> None:
        if (
            rollback_fails
            and Path(source_path) == target
            and Path(target_path) == source
        ):
            raise PermissionError("source locked")
        real_replace(source_path, target_path)

    monkeypatch.setattr(engine.timeline, "_write_order", fail_final_history_order)
    monkeypatch.setattr(engine.os, "replace", maybe_fail_move_rollback)

    expected_error = engine.MutationOutcomeUnconfirmed if rollback_fails else OSError
    with pytest.raises(expected_error):
        engine.timeline._execute_plan(plan)

    if rollback_fails:
        assert engine.timeline.paths.intent.exists()
        assert target.exists()
        return
    assert source.read_text(encoding="utf-8") == "History"
    assert not target.exists()
    assert engine.queue_files_in_order() == [current]
    assert engine.timeline.history_files() == [source]
    assert not engine.timeline.paths.intent.exists()


def build_archive_recovery_plan(engine):
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    earlier = engine.SPOKEN / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (current, waiting, earlier):
        path.write_text(path.stem, encoding="utf-8")
    current_id = speechicle_id(engine, current)
    waiting_id = speechicle_id(engine, waiting)
    earlier_id = speechicle_id(engine, earlier)
    engine.timeline.save_queue_order([current, waiting])
    engine.timeline.save_history_order([earlier])
    plan = TimelinePlan(
        kind="archive",
        moves=tuple(
            TimelineMove(
                TimelineLocation("queue", path.name),
                TimelineLocation("history", path.name),
            )
            for path in (current, waiting)
        ),
        previous_queue_ids=(current_id, waiting_id),
        previous_history_ids=(earlier_id,),
        queue_ids=(),
        history_ids=(waiting_id, current_id, earlier_id),
    )
    return plan, current, waiting, earlier


@pytest.mark.parametrize(
    "checkpoint",
    ["intent", "history_order", "queue_order", "move_1", "move_2", "converged"],
)
def test_timeline_plan_recovers_from_each_commit_checkpoint(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    engine = load_engine(f"super_speech_engine_plan_checkpoint_{checkpoint}")
    configure_runtime(engine, tmp_path)
    plan, current, waiting, earlier = build_archive_recovery_plan(engine)
    engine.timeline._write_intent(plan.intent_payload())
    move_count = {
        "intent": 0,
        "history_order": 0,
        "queue_order": 0,
        "move_1": 1,
        "move_2": 2,
        "converged": 2,
    }[checkpoint]
    for source in (current, waiting)[:move_count]:
        os.replace(source, engine.SPOKEN / source.name)
    if checkpoint != "intent":
        engine.timeline._write_order(engine.timeline.paths.history_order, list(plan.history_ids))
    if checkpoint not in {"intent", "history_order"}:
        engine.timeline._write_order(engine.timeline.paths.queue_order, list(plan.queue_ids))

    prepare_timeline(engine)

    assert engine.queue_files_in_order() == []
    assert engine.timeline.history_files() == [
        engine.SPOKEN / waiting.name,
        engine.SPOKEN / current.name,
        earlier,
    ]
    assert not engine.timeline.paths.intent.exists()


@pytest.mark.parametrize(
    "checkpoint", ["intent", "move", "queue_order", "history_order"]
)
def test_timeline_plan_recovers_a_voice_changing_promotion(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    engine = load_engine(f"super_speech_engine_plan_promote_{checkpoint}")
    configure_runtime(engine, tmp_path)
    source = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    target = engine.QUEUE / "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    source.write_text("Selected", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    source_id = speechicle_id(engine, source)
    waiting_id = speechicle_id(engine, waiting)
    plan = TimelinePlan(
        kind="promote",
        moves=(
            TimelineMove(
                TimelineLocation("history", source.name),
                TimelineLocation("queue", target.name),
            ),
        ),
        previous_queue_ids=(waiting_id,),
        previous_history_ids=(source_id,),
        queue_ids=(source_id, waiting_id),
        history_ids=(),
    )
    engine.timeline._write_intent(plan.intent_payload())
    if checkpoint != "intent":
        os.replace(source, target)
    if checkpoint in {"queue_order", "history_order"}:
        engine.timeline._write_order(engine.timeline.paths.queue_order, list(plan.queue_ids))
    if checkpoint == "history_order":
        engine.timeline._write_order(engine.timeline.paths.history_order, list(plan.history_ids))

    prepare_timeline(engine)

    assert target.read_text(encoding="utf-8") == "Selected"
    assert not source.exists()
    assert engine.queue_files_in_order() == [target, waiting]
    assert engine.timeline.history_files() == []
    assert not engine.timeline.paths.intent.exists()


@pytest.mark.parametrize(
    ("malformation", "error"),
    [
        ("duplicate_source", "duplicate move"),
        ("duplicate_target", "duplicate move"),
        ("backup_in_queue", "invalid timeline backup"),
        ("path_traversal", "invalid timeline source filename"),
        ("unknown_root", "invalid timeline source storage root"),
        ("duplicate_id", "duplicate final History IDs"),
        ("contradictory_orders", "same ID in both sections"),
        ("move_id_mismatch", "move files contradict"),
    ],
)
def test_timeline_plan_rejects_malformed_or_contradictory_payloads(
    tmp_path: Path,
    malformation: str,
    error: str,
) -> None:
    engine = load_engine(f"super_speech_engine_plan_invalid_{malformation}")
    configure_runtime(engine, tmp_path)
    plan, current, waiting, earlier = build_archive_recovery_plan(engine)
    payload = json.loads(json.dumps(plan.intent_payload()))
    if malformation == "duplicate_source":
        payload["moves"][1]["source"] = dict(payload["moves"][0]["source"])
        payload["moves"][1]["target"]["name"] = "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    elif malformation == "duplicate_target":
        payload["moves"][1]["target"] = dict(payload["moves"][0]["target"])
        payload["moves"][1]["source"]["name"] = "001-sp_00000000000000000000000000000001-bm_fable-say.txt"
    elif malformation == "backup_in_queue":
        payload["moves"][0]["backup"] = {
            "root": "queue",
            "name": f".{current.name}.test.duplicate",
        }
    elif malformation == "path_traversal":
        payload["moves"][0]["source"]["name"] = f"../{current.name}"
    elif malformation == "unknown_root":
        payload["moves"][0]["source"]["root"] = "failed"
    elif malformation == "duplicate_id":
        payload["history_ids"].append(payload["history_ids"][0])
    elif malformation == "move_id_mismatch":
        payload["moves"][1]["source"]["name"] = "004-sp_00000000000000000000000000000004-af_heart-say.txt"
        payload["moves"][1]["target"]["name"] = "004-sp_00000000000000000000000000000004-af_heart-say.txt"
    else:
        payload["queue_ids"] = [payload["previous_queue_ids"][0]]
    engine.timeline._write_intent(payload)

    with pytest.raises(RuntimeError, match=error):
        prepare_timeline(engine)

    assert engine.timeline.paths.intent.exists()
    assert current.exists()
    assert waiting.exists()
    assert earlier.exists()
    assert not list(engine.SPOKEN.glob("001-*.txt"))
    assert not list(engine.SPOKEN.glob("002-*.txt"))


@pytest.mark.parametrize("order_version", [1, 2])
@pytest.mark.parametrize("operation", ["archive", "archive_batch", "promote"])
def test_each_legacy_timeline_intent_adapts_to_common_recovery(
    tmp_path: Path,
    operation: str,
    order_version: int,
) -> None:
    engine = load_engine(
        f"super_speech_engine_legacy_{operation}_{order_version}"
    )
    configure_runtime(engine, tmp_path)
    source_directory = engine.SPOKEN if operation == "promote" else engine.QUEUE
    source = source_directory / "001-af_heart-say.txt"
    source.write_text("Legacy", encoding="utf-8")
    public_id = f"sp_{'1' * 32}"
    if order_version == 2:
        write_catalog(
            engine.timeline.paths.legacy_identity_index,
            IdentityCatalog(2, {1: public_id}),
        )
    saved_id = source.stem if order_version == 1 else public_id
    if operation == "archive":
        intent = {
            "version": 1,
            "operation": operation,
            "order_version": order_version,
            "name": source.name,
            "previous_history_ids": [],
            "desired_history_ids": [saved_id],
        }
        expected_root = engine.QUEUE
    else:
        intent = {
            "version": 1,
            "operation": operation,
            "order_version": order_version,
            "moves": [{"source": source.name, "target": source.name}],
            "queue_ids": [] if operation == "archive_batch" else [saved_id],
            "history_ids": [saved_id] if operation == "archive_batch" else [],
        }
        expected_root = engine.SPOKEN if operation == "archive_batch" else engine.QUEUE
    engine.timeline._write_intent(intent)

    prepare_timeline(engine)

    final_files = list(expected_root.glob("*.txt"))
    assert len(final_files) == 1
    assert final_files[0].read_text(encoding="utf-8") == "Legacy"
    final_name = engine.SpeechicleFilename.parse(final_files[0].name)
    if order_version == 2:
        assert final_name.public_id == public_id
    assert not engine.timeline.paths.intent.exists()
    order_path = (
        engine.timeline.paths.history_order if operation == "archive_batch" else engine.timeline.paths.queue_order
    )
    assert json.loads(order_path.read_text(encoding="utf-8"))["version"] == 2


def test_legacy_promotion_finishes_an_intermediate_voice_rename(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_legacy_promotion_voice_rename")
    configure_runtime(engine, tmp_path)
    renamed_source = engine.QUEUE / "001-af_heart-say.txt"
    target = engine.QUEUE / "001-bm_fable-say.txt"
    renamed_source.write_text("Selected", encoding="utf-8")
    engine.timeline._write_intent(
        {
            "version": 1,
            "operation": "promote",
            "moves": [
                {
                    "source": renamed_source.name,
                    "target": target.name,
                    "backup": None,
                }
            ],
            "queue_ids": [target.stem],
            "history_ids": [],
        }
    )

    prepare_timeline(engine)

    final_files = list(engine.QUEUE.glob("*.txt"))
    assert len(final_files) == 1
    assert final_files[0].read_text(encoding="utf-8") == "Selected"
    assert engine.SpeechicleFilename.parse(final_files[0].name).voice == "bm_fable"
    assert not renamed_source.exists()
    assert engine.queue_files_in_order() == final_files
    assert not engine.timeline.paths.intent.exists()


def test_unsafe_legacy_intent_is_retained_without_partial_application(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_legacy_unsafe_intent")
    configure_runtime(engine, tmp_path)
    first = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    unsafe = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    first.write_text("First", encoding="utf-8")
    unsafe.write_text("Safe", encoding="utf-8")
    first_id = speechicle_id(engine, first)
    unsafe_id = speechicle_id(engine, unsafe)
    write_upgrade_catalog(engine, first, unsafe)
    engine.timeline._write_intent(
        {
            "version": 1,
            "operation": "archive_batch",
            "order_version": 2,
            "moves": [
                {"source": first.name, "target": first.name},
                {"source": f"../{unsafe.name}", "target": unsafe.name},
            ],
            "queue_ids": [],
            "history_ids": [unsafe_id, first_id],
        }
    )

    with pytest.raises(RuntimeError):
        prepare_timeline(engine)

    assert engine.timeline.paths.intent.exists()
    assert first.read_text(encoding="utf-8") == "First"
    assert unsafe.read_text(encoding="utf-8") == "Safe"
    assert not (engine.SPOKEN / first.name).exists()
    assert not (engine.SPOKEN / unsafe.name).exists()


def test_timeline_plan_retains_journal_until_backup_cleanup_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_plan_backup_cleanup")
    configure_runtime(engine, tmp_path)
    source = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    target = engine.SPOKEN / source.name
    backup = engine.SPOKEN / f".{source.name}.test.duplicate"
    source.write_text("Current", encoding="utf-8")
    row_id = speechicle_id(engine, source)
    target.write_text("Duplicate", encoding="utf-8")
    plan = TimelinePlan(
        kind="archive",
        moves=(
            TimelineMove(
                TimelineLocation("queue", source.name),
                TimelineLocation("history", target.name),
                TimelineLocation("history", backup.name),
            ),
        ),
        previous_queue_ids=(row_id,),
        previous_history_ids=(),
        queue_ids=(),
        history_ids=(row_id,),
    )
    engine.timeline._write_intent(plan.intent_payload())
    real_unlink = Path.unlink

    def fail_backup_cleanup(path: Path, *args, **kwargs) -> None:
        if path == backup:
            raise PermissionError("backup locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)

    with pytest.raises(PermissionError, match="backup locked"):
        prepare_timeline(engine)

    assert engine.timeline.paths.intent.exists()
    assert target.read_text(encoding="utf-8") == "Current"
    assert backup.read_text(encoding="utf-8") == "Duplicate"

    monkeypatch.setattr(Path, "unlink", real_unlink)
    prepare_timeline(engine)
    assert not backup.exists()
    assert not engine.timeline.paths.intent.exists()


def test_timeline_plan_retains_journal_when_a_row_cannot_be_recovered(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_plan_missing_row")
    configure_runtime(engine, tmp_path)
    plan, current, waiting, earlier = build_archive_recovery_plan(engine)
    engine.timeline._write_intent(plan.intent_payload())
    current.unlink()

    with pytest.raises(RuntimeError, match="pending timeline row is missing"):
        prepare_timeline(engine)

    assert engine.timeline.paths.intent.exists()
    assert waiting.exists()
    assert earlier.exists()


def test_startup_completes_an_interrupted_clear_batch(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_batch_recovery")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-af_heart-say.txt"
    earlier = engine.SPOKEN / "003-af_heart-say.txt"
    for path, text in (
        (current, "Current"),
        (waiting, "Waiting"),
        (earlier, "Earlier"),
    ):
        path.write_text(text, encoding="utf-8")
    public_ids = {sequence: f"sp_{sequence:032x}" for sequence in (1, 2, 3)}
    write_catalog(
        engine.timeline.paths.legacy_identity_index,
        IdentityCatalog(4, public_ids),
    )
    engine.timeline._write_order(
        engine.timeline.paths.queue_order, [public_ids[1], public_ids[2]], 2
    )
    engine.timeline._write_order(engine.timeline.paths.history_order, [public_ids[3]], 2)
    engine.timeline._write_intent(
        {
            "version": 1,
            "operation": "archive_batch",
            "order_version": 2,
            "moves": [
                {"source": current.name, "target": current.name},
                {"source": waiting.name, "target": waiting.name},
            ],
            "previous_queue_ids": [
                public_ids[1],
                public_ids[2],
            ],
            "previous_history_ids": [public_ids[3]],
            "queue_ids": [],
            "history_ids": [
                public_ids[2],
                public_ids[1],
                public_ids[3],
            ],
        }
    )
    os.replace(current, engine.SPOKEN / current.name)

    prepare_timeline(engine)

    assert engine.queue_files_in_order() == []
    assert [
        path.read_text(encoding="utf-8") for path in engine.timeline.history_files()
    ] == [
        "Waiting",
        "Current",
        "Earlier",
    ]
    assert not engine.timeline.paths.intent.exists()


def test_archive_succeeds_when_committed_intent_cleanup_is_deferred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_deferred_cleanup")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    prepare_timeline(engine)
    waiting = engine.enqueue_text("Waiting", "af_heart")
    real_unlink = Path.unlink

    def locked_intent(path: Path, *args, **kwargs) -> None:
        if path == engine.timeline.paths.intent:
            raise PermissionError("locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_intent)
    assert engine.archive(current)
    assert not current.exists()
    assert (engine.SPOKEN / current.name).exists()
    assert engine.timeline.paths.intent.exists()
    pending_intent = engine.timeline.paths.intent.read_text(encoding="utf-8")
    pending_payload = json.loads(pending_intent)
    assert pending_payload["version"] == 3
    assert pending_payload["operation"] == "timeline_plan"
    assert pending_payload["moves"][0]["source"] == {
        "root": "queue",
        "name": current.name,
    }
    assert pending_payload["moves"][0]["target"] == {
        "root": "history",
        "name": current.name,
    }
    with pytest.raises(PermissionError, match="locked"):
        engine.archive(waiting)
    assert waiting.exists()
    assert engine.timeline.paths.intent.read_text(encoding="utf-8") == pending_intent

    monkeypatch.setattr(Path, "unlink", real_unlink)
    prepare_timeline(engine)
    assert not engine.timeline.paths.intent.exists()
    assert engine.timeline.history_files() == [engine.SPOKEN / current.name]


def test_pending_timeline_plan_blocks_a_different_mutation_until_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_pending_plan_gate")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    earlier = engine.SPOKEN / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    earlier.write_text("Earlier", encoding="utf-8")
    prepare_timeline(engine)
    real_unlink = Path.unlink

    def locked_intent(path: Path, *args, **kwargs) -> None:
        if path == engine.timeline.paths.intent:
            raise PermissionError("locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_intent)
    assert engine.archive(current)
    assert engine.timeline.paths.intent.exists()
    request_id = request_mutation(
        engine,
        "delete",
        id=speechicle_id(engine, earlier),
    )

    engine.process_mutation_requests(queue.Queue(), engine.State())

    result = rejected_result(engine, request_id)
    assert "locked" in str(result["error"])
    assert earlier.exists()
    assert engine.timeline.paths.intent.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry_id = request_mutation(
        engine,
        "delete",
        id=speechicle_id(engine, earlier),
    )
    engine.process_mutation_requests(queue.Queue(), engine.State())
    committed_result(engine, retry_id)
    assert not earlier.exists()
    assert not engine.timeline.paths.intent.exists()


def test_archive_rollback_does_not_resurrect_an_already_archived_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_no_resurrection")
    configure_runtime(engine, tmp_path)
    archived = engine.SPOKEN / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    blocked = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
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
    duplicate_queue = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    duplicate_history = engine.SPOKEN / duplicate_queue.name
    blocked = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    duplicate_queue.write_text("Active copy", encoding="utf-8")
    duplicate_history.write_text("History copy", encoding="utf-8")
    blocked.write_text("Blocked", encoding="utf-8")
    engine.timeline.save_queue_order([duplicate_queue, blocked])
    engine.timeline.save_history_order([duplicate_history])
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
    assert engine.timeline.history_files() == [migrated_history]
    assert not engine.timeline.paths.intent.exists()


def test_unconfirmed_archive_rollback_retains_its_recovery_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_archive_unconfirmed_recovery")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    engine.timeline.save_queue_order([current, waiting])
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
    assert engine.timeline.paths.intent.exists()

    monkeypatch.setattr(engine.os, "replace", real_replace)
    prepare_timeline(engine)

    assert engine.queue_files_in_order() == []
    assert engine.timeline.history_files() == [
        engine.SPOKEN / waiting.name,
        engine.SPOKEN / current.name,
    ]
    assert not engine.timeline.paths.intent.exists()


def test_clear_archives_a_paused_current_and_publishes_idle(tmp_path: Path) -> None:
    engine = load_engine("super_speech_engine_clear_paused_current")
    configure_runtime(engine, tmp_path)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    current.write_text("Paused current", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    engine.PAUSE.touch()

    assert engine.do_clear(queue.Queue(), state)
    engine.publish_status(state, force=True)
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
    interrupted.timeline._write_order(
        interrupted.timeline.paths.queue_order, [queued.stem], 1
    )
    interrupted.timeline._write_order(
        interrupted.timeline.paths.history_order,
        [malformed.stem, collision.stem],
        1,
    )

    def stop_after_journal(_migration) -> None:
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(
        interrupted.timeline, "_converge_embed", stop_after_journal
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        prepare_timeline(interrupted)
    saved_intent = json.loads(
        interrupted.timeline.paths.intent.read_text(encoding="utf-8")
    )
    saved_queue_ids = [
        interrupted.SpeechicleFilename.parse(item["target_name"]).public_id
        for item in saved_intent["queue_files"]
    ]
    saved_history_ids = [
        interrupted.SpeechicleFilename.parse(item["target_name"]).public_id
        for item in saved_intent["history_files"]
    ]

    recovered = load_engine("super_speech_engine_identity_recovered")
    configure_runtime(recovered, tmp_path)
    prepare_timeline(recovered)

    assert not recovered.timeline.paths.legacy_identity_index.exists()
    assert json.loads(
        recovered.timeline.paths.queue_order.read_text(encoding="utf-8")
    ) == {
        "version": 2,
        "ids": saved_queue_ids,
    }
    assert json.loads(
        recovered.timeline.paths.history_order.read_text(encoding="utf-8")
    ) == {
        "version": 2,
        "ids": saved_history_ids,
    }
    files = [
        *recovered.QUEUE.glob("*.txt"),
        *recovered.SPOKEN.glob("*.txt"),
        *recovered.FAILED.glob("*.txt"),
    ]
    sequences = [recovered.SpeechicleFilename.parse(path.name).sequence for path in files]
    assert len(set(sequences)) == len(sequences)
    assert not recovered.timeline.paths.intent.exists()


def test_deleted_public_identity_and_sequence_are_not_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_identity_delete")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    first = engine.enqueue_text("First", "af_heart")
    first_id = speechicle_id(engine, first)
    first_sequence = engine.SpeechicleFilename.parse(first.name).sequence
    assert engine.archive(first)
    engine.apply_delete_mutation(
        build_mutation(engine, "delete", id=first_id)
    )

    second = engine.enqueue_text("Second", "af_heart")
    second_id = speechicle_id(engine, second)

    assert engine.SpeechicleFilename.parse(second.name).sequence == first_sequence + 1
    assert second_id != first_id
    assert engine.timeline.find(engine.QUEUE, first_id) is None
    assert engine.timeline.find(engine.SPOKEN, first_id) is None

    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    request_id = request_mutation(engine, "play", id=first_id, voice=None)
    assert engine.process_mutation_requests(queue.Queue(), engine.State()) is None
    result = rejected_result(engine, request_id)
    assert "Speechicle not found in Current, Waiting, or History" in str(
        result["error"]
    )


def test_voice_and_history_lifecycle_preserve_one_public_identity(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_identity_lifecycle")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    original = engine.enqueue_text("Same words", "af_heart", 250)
    public_id = speechicle_id(engine, original)

    changed = engine.timeline.replace_queue_voice(original, "bm_fable")
    assert speechicle_id(engine, changed) == public_id
    assert engine.archive(changed)
    archived = engine.timeline.find(engine.SPOKEN, public_id)
    assert archived is not None

    replayed, _ = engine.timeline.promote_history(archived, "af_heart")

    assert speechicle_id(engine, replayed) == public_id
    assert replayed.name.endswith("-af_heart-g250-say.txt")


def test_voice_change_does_not_rewrite_stable_queue_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_voice_order_is_stable")
    configure_runtime(engine, tmp_path)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    prepare_timeline(engine)
    current = engine.enqueue_text("Current", "af_heart")
    waiting = engine.enqueue_text("Waiting", "af_heart")
    engine.timeline.save_queue_order([current, waiting])
    saved_order = engine.timeline.paths.queue_order.read_bytes()
    monkeypatch.setattr(
        engine.timeline,
        "_write_order",
        lambda *_args: (_ for _ in ()).throw(AssertionError("order was rewritten")),
    )

    changed = engine.timeline.replace_queue_voice(waiting, "bm_fable")

    assert engine.timeline.paths.queue_order.read_bytes() == saved_order
    assert engine.queue_files_in_order() == [current, changed]


def test_skip_invalidates_buffered_audio_by_removing_its_claim(
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_skip_claim_generation")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    current = engine.enqueue_text("Current", "af_heart")
    state = engine.State()
    _, generation = engine._record_claim(state, current)
    set_current(engine, state, current)

    assert engine.finish_chunk_playback(current, "skip", True, state)

    assert engine.buffered_piece_is_stale(state, current.name, generation)


def test_mutations_commit_and_publish_results_in_one_fifo_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_mutation_fifo")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    first = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    second = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    current.write_text("Current", encoding="utf-8")
    first.write_text("First waiting", encoding="utf-8")
    second.write_text("Second waiting", encoding="utf-8")
    current_id = speechicle_id(engine, current)
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
        engine, "play", id=current_id, voice=None
    )
    published: list[str] = []
    real_publish = engine.publish_mutation_result

    def publish(request_id: str, *args, **kwargs) -> bool:
        published.append(request_id)
        return real_publish(request_id, *args, **kwargs)

    monkeypatch.setattr(engine, "publish_mutation_result", publish)

    assert engine.process_mutation_requests(queue.Queue(), engine.State()) == "queue_changed"

    assert published == [move_request, play_request]
    assert committed_result(engine, move_request)["snapshot"]["current"]["id"] == current_id
    play_result = committed_result(engine, play_request)
    assert play_result["result_id"] == current_id
    assert play_result["snapshot"]["current"]["id"] == current_id


def test_later_waiting_move_does_not_hide_an_earlier_playback_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_mutation_effect_priority")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    selected = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    waiting = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (current, selected, waiting):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)
    selected_id = speechicle_id(engine, selected)
    play_request = request_mutation(
        engine, "play", id=selected_id, voice=None
    )
    move_request = request_mutation(
        engine,
        "move",
        section="waiting",
        id=speechicle_id(engine, waiting),
        before_id=None,
    )

    assert engine.process_mutation_requests(queue.Queue(), state) == "select"
    assert committed_result(engine, play_request)["result_id"] == selected_id
    committed_result(engine, move_request)
    assert state.current_projection is not None
    assert state.current_projection.filename == selected.name


def test_every_mutation_outcome_contains_an_authoritative_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_mutation_outcomes")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    current = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    waiting = engine.QUEUE / "002-sp_00000000000000000000000000000002-bm_fable-say.txt"
    current.write_text("Current", encoding="utf-8")
    waiting.write_text("Waiting", encoding="utf-8")
    state = engine.State()
    set_current(engine, state, current)

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


def test_enqueue_mutation_publishes_source_and_stable_result_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("super_speech_engine_enqueue_mutation")
    configure_runtime(engine, tmp_path)
    prepare_timeline(engine)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart"}
    state = engine.State()

    request_id = request_mutation(
        engine,
        "enqueue",
        text="A pasted article",
        voice="af_heart",
        source="Manual",
    )

    assert engine.process_mutation_requests(queue.Queue(), state) == "queue_changed"
    result = committed_result(engine, request_id)
    current = result["snapshot"]["current"]
    assert result["result_id"] == current["id"]
    assert current["text"] == "A pasted article"
    assert current["voice"] == "af_heart"
    assert current["source"] == "Manual"


def test_timeline_revision_changes_only_with_the_timeline_and_survives_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = load_engine("super_speech_engine_timeline_revision")
    configure_runtime(engine, tmp_path)
    monkeypatch.setattr(engine, "engine_is_running", lambda: True)
    engine.AVAILABLE_VOICES = {"af_heart", "bm_fable"}
    first = engine.QUEUE / "001-sp_00000000000000000000000000000001-af_heart-say.txt"
    second = engine.QUEUE / "002-sp_00000000000000000000000000000002-af_heart-say.txt"
    third = engine.QUEUE / "003-sp_00000000000000000000000000000003-af_heart-say.txt"
    for path in (first, second, third):
        path.write_text(path.stem, encoding="utf-8")
    state = engine.State()
    initial = engine.publish_status(state, force=True)
    assert initial is not None
    assert initial["timeline_revision"] == 0

    assert state.current_projection is not None
    assert engine.update_current_piece(
        state,
        state.current_projection.filename,
        1,
        0,
        len(state.current_projection.text),
    )
    engine.PAUSE.touch()
    progress = engine.publish_status(state, force=True)
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
    after_restart = engine.publish_status(restarted, force=True)
    assert after_restart is not None
    assert after_restart["timeline_revision"] == 2
