from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from speechicle_identity import IdentityCatalog, SpeechicleFilename, write_catalog


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
    engine.QUEUE_ORDER = tmp_path / "queue-order.json"
    engine.HISTORY_ORDER = tmp_path / "history-order.json"
    engine.STATUS = tmp_path / "status.json"
    engine.STATUS_FAILURE = tmp_path / "status.failed"
    engine.INSTANCE_LOCK = tmp_path / "engine.lock"
    engine.TIMELINE_LOCK = tmp_path / "timeline.lock"
    engine.TIMELINE_INTENT = tmp_path / "timeline-intent.json"
    engine.IDENTITY_INDEX = tmp_path / "speechicle-index.json"
    engine._order_cache.clear()


def canonical_name(
    sequence: int,
    digit: str,
    voice: str = "af_heart",
    gap_ms: int | None = None,
) -> str:
    return SpeechicleFilename(sequence, f"sp_{digit * 32}", voice, gap_ms).render()


def prepare(engine) -> None:
    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()


def read_ids(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["ids"]


def planned_legacy_embed(engine, *, write_intent: bool = True):
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    engine.FAILED.mkdir()
    first_id = f"sp_{'1' * 32}"
    second_id = f"sp_{'2' * 32}"
    third_id = f"sp_{'3' * 32}"
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.SPOKEN / "002-bm_fable-say.txt"
    third = engine.FAILED / "003-af_bella-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    third.write_text("Third", encoding="utf-8")
    write_catalog(
        engine.IDENTITY_INDEX,
        IdentityCatalog(4, {1: first_id, 2: second_id, 3: third_id}),
    )
    engine.QUEUE_ORDER.write_text(
        json.dumps({"version": 2, "ids": [first_id]}), encoding="utf-8"
    )
    engine.HISTORY_ORDER.write_text(
        json.dumps({"version": 2, "ids": [second_id]}), encoding="utf-8"
    )
    migration = engine.plan_embed_public_ids(
        engine.QUEUE,
        engine.SPOKEN,
        engine.FAILED,
        engine.QUEUE_ORDER,
        engine.HISTORY_ORDER,
        engine.IDENTITY_INDEX,
        engine.DEFAULT_VOICE,
    )
    assert migration is not None
    if write_intent:
        engine._write_timeline_intent(migration.intent_payload())
    return migration


def apply_embed_moves(engine, migration, count: int) -> None:
    planned = [
        (root, item)
        for root, files in engine._embed_target_sections(migration)
        for item in files
    ]
    for target_root, item in planned[:count]:
        source = engine._migration_root_path(item.source_root) / item.source_name
        target = engine._migration_root_path(target_root) / item.target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target:
            os.replace(source, target)


def test_preparation_requires_the_held_instance_lock(tmp_path: Path) -> None:
    engine = load_engine("embedded_identity_lock")
    configure_runtime(engine, tmp_path)

    with pytest.raises(RuntimeError, match="held engine instance lock"):
        engine.prepare_timeline_storage(engine.EngineInstanceLock())


def test_canonical_preparation_repairs_membership_and_removes_stale_catalog(
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_canonical_repair")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    current = engine.QUEUE / canonical_name(4, "4")
    waiting = engine.QUEUE / canonical_name(8, "8", "bm_fable")
    newer = engine.SPOKEN / canonical_name(7, "7")
    older = engine.SPOKEN / canonical_name(2, "2")
    for path in (current, waiting, newer, older):
        path.write_text(path.stem, encoding="utf-8")
    engine.QUEUE_ORDER.write_text(
        json.dumps({"version": 2, "ids": [current.name.split("-")[1], f"sp_{'f' * 32}"]}),
        encoding="utf-8",
    )
    engine.HISTORY_ORDER.write_text(
        json.dumps({"version": 2, "ids": [older.name.split("-")[1]]}),
        encoding="utf-8",
    )
    engine.IDENTITY_INDEX.write_text("corrupt stale catalog", encoding="utf-8")

    prepare(engine)

    assert read_ids(engine.QUEUE_ORDER) == [
        current.name.split("-")[1],
        waiting.name.split("-")[1],
    ]
    assert read_ids(engine.HISTORY_ORDER) == [
        newer.name.split("-")[1],
        older.name.split("-")[1],
    ]
    assert not engine.IDENTITY_INDEX.exists()


def test_interrupted_embed_converges_before_deleting_catalog(tmp_path: Path) -> None:
    engine = load_engine("embedded_identity_resume")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    first_id = f"sp_{'1' * 32}"
    second_id = f"sp_{'2' * 32}"
    first = engine.QUEUE / "001-af_heart-say.txt"
    second = engine.SPOKEN / "002-bm_fable-say.txt"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    write_catalog(
        engine.IDENTITY_INDEX,
        IdentityCatalog(3, {1: first_id, 2: second_id}),
    )
    engine.QUEUE_ORDER.write_text(
        json.dumps({"version": 2, "ids": [first_id]}), encoding="utf-8"
    )
    engine.HISTORY_ORDER.write_text(
        json.dumps({"version": 2, "ids": [second_id]}), encoding="utf-8"
    )
    migration = engine.plan_embed_public_ids(
        engine.QUEUE,
        engine.SPOKEN,
        engine.FAILED,
        engine.QUEUE_ORDER,
        engine.HISTORY_ORDER,
        engine.IDENTITY_INDEX,
        engine.DEFAULT_VOICE,
    )
    assert migration is not None
    engine._write_timeline_intent(migration.intent_payload())
    first_move = migration.queue_files[0]
    os.replace(first, engine.QUEUE / first_move.target_name)

    prepare(engine)

    assert {path.name for path in engine.QUEUE.glob("*.txt")} == {
        migration.queue_files[0].target_name
    }
    assert {path.name for path in engine.SPOKEN.glob("*.txt")} == {
        migration.history_files[0].target_name
    }
    assert not engine.TIMELINE_INTENT.exists()
    assert not engine.IDENTITY_INDEX.exists()


@pytest.mark.parametrize(
    "checkpoint",
    [
        "intent",
        "first_rename",
        "all_renames",
        "history_order",
        "queue_order",
        "catalog_deleted",
    ],
)
def test_embed_recovery_converges_from_each_durable_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    checkpoint: str,
) -> None:
    engine = load_engine(f"embedded_identity_checkpoint_{checkpoint}")
    configure_runtime(engine, tmp_path)
    migration = planned_legacy_embed(engine, write_intent=False)
    monkeypatch.setattr(engine, "plan_embed_public_ids", lambda *_args, **_kwargs: migration)
    real_replace = engine.os.replace
    real_validate = engine._validate_embed_inventory
    real_write_order = engine._write_order_payload
    real_unlink = Path.unlink
    text_moves = 0

    if checkpoint == "intent":
        def crash_after_intent(_migration) -> None:
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(engine, "_converge_embed_public_ids", crash_after_intent)
    elif checkpoint == "first_rename":
        def crash_after_first_rename(source, target) -> None:
            nonlocal text_moves
            real_replace(source, target)
            if Path(source).suffix == ".txt" and Path(target).suffix == ".txt":
                text_moves += 1
                if text_moves == 1:
                    raise RuntimeError("simulated crash")

        monkeypatch.setattr(engine.os, "replace", crash_after_first_rename)
    elif checkpoint == "all_renames":
        def crash_before_final_validation(migration_arg, *, final: bool) -> None:
            if final:
                raise RuntimeError("simulated crash")
            real_validate(migration_arg, final=final)

        monkeypatch.setattr(
            engine, "_validate_embed_inventory", crash_before_final_validation
        )
    elif checkpoint in {"history_order", "queue_order"}:
        crash_path = (
            engine.HISTORY_ORDER if checkpoint == "history_order" else engine.QUEUE_ORDER
        )

        def crash_after_order(path: Path, ids: list[str], version: int) -> None:
            real_write_order(path, ids, version)
            if path == crash_path:
                raise RuntimeError("simulated crash")

        monkeypatch.setattr(engine, "_write_order_payload", crash_after_order)
    else:
        def crash_after_catalog_delete(path: Path, *args, **kwargs) -> None:
            real_unlink(path, *args, **kwargs)
            if path == engine.IDENTITY_INDEX:
                raise RuntimeError("simulated crash")

        monkeypatch.setattr(Path, "unlink", crash_after_catalog_delete)

    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()
    assert engine.TIMELINE_INTENT.exists()
    monkeypatch.undo()

    recovered = load_engine(f"embedded_identity_checkpoint_recovered_{checkpoint}")
    configure_runtime(recovered, tmp_path)
    prepare(recovered)

    assert {path.name for path in recovered.QUEUE.glob("*.txt")} == {
        item.target_name for item in migration.queue_files
    }
    assert {path.name for path in recovered.SPOKEN.glob("*.txt")} == {
        item.target_name for item in migration.history_files
    }
    assert {path.name for path in recovered.FAILED.glob("*.txt")} == {
        item.target_name for item in migration.failed_files
    }
    assert read_ids(recovered.QUEUE_ORDER) == list(migration.queue_ids)
    assert read_ids(recovered.HISTORY_ORDER) == list(migration.history_ids)
    assert not recovered.IDENTITY_INDEX.exists()
    assert not recovered.TIMELINE_INTENT.exists()


def test_embed_recovery_is_repeatable_after_journal_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_journal_cleanup")
    configure_runtime(engine, tmp_path)
    migration = planned_legacy_embed(engine)
    real_unlink = Path.unlink

    def block_journal(path: Path, *args, **kwargs) -> None:
        if path == engine.TIMELINE_INTENT:
            raise PermissionError("journal locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", block_journal)
    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(PermissionError, match="journal locked"):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()

    assert engine.TIMELINE_INTENT.exists()
    assert not engine.IDENTITY_INDEX.exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    prepare(engine)
    assert read_ids(engine.QUEUE_ORDER) == list(migration.queue_ids)
    assert read_ids(engine.HISTORY_ORDER) == list(migration.history_ids)
    assert not engine.TIMELINE_INTENT.exists()


def test_embed_hash_mismatch_stops_before_another_file_moves(tmp_path: Path) -> None:
    engine = load_engine("embedded_identity_hash_gate")
    configure_runtime(engine, tmp_path)
    migration = planned_legacy_embed(engine)
    apply_embed_moves(engine, migration, 1)
    moved = engine.QUEUE / migration.queue_files[0].target_name
    moved.write_text("Tampered", encoding="utf-8")
    untouched = engine.SPOKEN / migration.history_files[0].source_name
    original_orders = {
        engine.QUEUE_ORDER: engine.QUEUE_ORDER.read_bytes(),
        engine.HISTORY_ORDER: engine.HISTORY_ORDER.read_bytes(),
    }

    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="hash changed"):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()

    assert moved.read_text(encoding="utf-8") == "Tampered"
    assert untouched.read_text(encoding="utf-8") == "Second"
    assert all(path.read_bytes() == payload for path, payload in original_orders.items())
    assert engine.IDENTITY_INDEX.exists()
    assert engine.TIMELINE_INTENT.exists()


def test_embed_recovery_accepts_an_already_removed_exact_replay_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_removed_duplicate")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    active = engine.QUEUE / "003-af_heart-say.txt"
    duplicate = engine.SPOKEN / active.name
    promoted = engine.SPOKEN / "004-bm_fable-say.txt"
    older = engine.SPOKEN / "001-af_bella-say.txt"
    active.write_bytes(b"same")
    duplicate.write_bytes(b"same")
    promoted.write_bytes(b"promoted")
    older.write_bytes(b"older")
    engine.QUEUE_ORDER.write_text(
        json.dumps({"version": 1, "ids": [active.stem]}), encoding="utf-8"
    )
    engine.HISTORY_ORDER.write_text(
        json.dumps(
            {
                "version": 1,
                "ids": [promoted.stem, duplicate.stem, older.stem],
            }
        ),
        encoding="utf-8",
    )
    migration = engine.plan_embed_public_ids(
        engine.QUEUE,
        engine.SPOKEN,
        engine.FAILED,
        engine.QUEUE_ORDER,
        engine.HISTORY_ORDER,
        engine.IDENTITY_INDEX,
        engine.DEFAULT_VOICE,
    )
    assert migration is not None and len(migration.removals) == 1
    monkeypatch.setattr(engine, "plan_embed_public_ids", lambda *_args, **_kwargs: migration)
    real_unlink = Path.unlink

    def crash_after_duplicate_removal(path: Path, *args, **kwargs) -> None:
        real_unlink(path, *args, **kwargs)
        if path == duplicate:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(Path, "unlink", crash_after_duplicate_removal)
    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()
    assert not duplicate.exists()
    assert engine.TIMELINE_INTENT.exists()
    monkeypatch.undo()

    recovered = load_engine("embedded_identity_removed_duplicate_recovered")
    configure_runtime(recovered, tmp_path)
    prepare(recovered)

    assert not recovered.TIMELINE_INTENT.exists()
    queue_files = {path.name: path for path in recovered.QUEUE.glob("*.txt")}
    history_files = {path.name: path for path in recovered.SPOKEN.glob("*.txt")}
    assert set(queue_files) == {item.target_name for item in migration.queue_files}
    assert set(history_files) == {
        item.target_name for item in migration.history_files
    }
    final_text = [
        path.read_bytes() for path in [*queue_files.values(), *history_files.values()]
    ]
    assert sorted(final_text) == sorted([b"same", b"promoted", b"older"])
    assert read_ids(recovered.QUEUE_ORDER) == list(migration.queue_ids)
    assert read_ids(recovered.HISTORY_ORDER) == list(migration.history_ids)


def test_embed_keeps_catalog_and_journal_until_order_validation_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_catalog_timing")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    public_id = f"sp_{'3' * 32}"
    legacy = engine.QUEUE / "003-af_heart-say.txt"
    legacy.write_text("Keep recovery evidence", encoding="utf-8")
    write_catalog(engine.IDENTITY_INDEX, IdentityCatalog(4, {3: public_id}))
    engine.QUEUE_ORDER.write_text(
        json.dumps({"version": 2, "ids": [public_id]}), encoding="utf-8"
    )

    def fail_normalization() -> None:
        raise RuntimeError("sidecar unavailable")

    monkeypatch.setattr(engine, "_normalize_canonical_orders", fail_normalization)

    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="sidecar unavailable"):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()

    assert engine.TIMELINE_INTENT.exists()
    assert engine.IDENTITY_INDEX.exists()


def test_catalog_free_allocation_and_metadata_use_canonical_filenames(
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_steady_state")
    configure_runtime(engine, tmp_path)
    prepare(engine)

    first = engine.enqueue_text("First", "af_heart", 250)
    first_filename = SpeechicleFilename.parse(first.name)
    assert not engine.IDENTITY_INDEX.exists()
    assert engine.public_id_for_path(first) == first_filename.public_id
    assert engine.voice_from_name(first.name) == "af_heart"
    assert engine.gap_from_name(first.name) == 0.25

    first.unlink()
    second = engine.enqueue_text("Second", "bm_fable")
    second_filename = SpeechicleFilename.parse(second.name)
    assert second_filename.sequence == first_filename.sequence
    assert second_filename.public_id != first_filename.public_id
    assert engine._find_chunk(engine.QUEUE, second_filename.public_id) == second
    changed = engine._replace_queue_voice(second, "af_heart")
    changed_filename = SpeechicleFilename.parse(changed.name)
    assert changed_filename.public_id == second_filename.public_id
    assert changed_filename.voice == "af_heart"
    assert not engine.IDENTITY_INDEX.exists()


def test_missing_catalog_rejects_pending_v2_plan_before_storage_write(
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_missing_v2_catalog")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    source = engine.QUEUE / "001-af_heart-say.txt"
    source.write_text("Do not move", encoding="utf-8")
    public_id = f"sp_{'6' * 32}"
    intent = {
        "version": 2,
        "operation": "timeline_plan",
        "moves": [
            {
                "source": {"root": "queue", "name": source.name},
                "target": {"root": "history", "name": source.name},
                "backup": None,
                "preserve_existing_target": False,
            }
        ],
        "previous_queue_ids": [public_id],
        "previous_history_ids": [],
        "queue_ids": [],
        "history_ids": [public_id],
    }
    engine.TIMELINE_INTENT.write_text(json.dumps(intent), encoding="utf-8")

    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="requires the identity catalog"):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()

    assert source.exists()
    assert not (engine.SPOKEN / source.name).exists()
    assert json.loads(engine.TIMELINE_INTENT.read_text(encoding="utf-8")) == intent


@pytest.mark.parametrize(
    ("sidecar_problem", "error"),
    [
        ("missing_catalog", "version-2 speech order requires the identity catalog"),
        ("duplicate_ids", "invalid speech order"),
    ],
)
def test_fresh_cutover_rejects_unprovable_v2_orders_before_writing(
    tmp_path: Path,
    sidecar_problem: str,
    error: str,
) -> None:
    engine = load_engine(f"embedded_identity_unprovable_{sidecar_problem}")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    legacy = engine.QUEUE / "001-af_heart-say.txt"
    legacy.write_text("Keep the original identity", encoding="utf-8")
    public_id = f"sp_{'7' * 32}"
    ids = [public_id] if sidecar_problem == "missing_catalog" else [public_id, public_id]
    engine.QUEUE_ORDER.write_text(
        json.dumps({"version": 2, "ids": ids}), encoding="utf-8"
    )
    original_order = engine.QUEUE_ORDER.read_bytes()

    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match=error):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()

    assert legacy.read_text(encoding="utf-8") == "Keep the original identity"
    assert engine.QUEUE_ORDER.read_bytes() == original_order
    assert not engine.IDENTITY_INDEX.exists()
    assert not engine.TIMELINE_INTENT.exists()


def test_fresh_cutover_refuses_to_generate_ids_beside_a_durable_mutation(
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_durable_mutation_gate")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    legacy = engine.QUEUE / "001-af_heart-say.txt"
    legacy.write_text("Do not guess", encoding="utf-8")
    durable_mutation = tmp_path / "MUTATION.pending.json"
    durable_mutation.write_text("{}", encoding="utf-8")

    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="durable mutation"):
            engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()

    assert legacy.read_text(encoding="utf-8") == "Do not guess"
    assert durable_mutation.read_text(encoding="utf-8") == "{}"
    assert not engine.IDENTITY_INDEX.exists()
    assert not engine.TIMELINE_INTENT.exists()


def test_fresh_v1_storage_cuts_over_directly_to_canonical_filenames(
    tmp_path: Path,
) -> None:
    engine = load_engine("embedded_identity_direct_v1_cutover")
    configure_runtime(engine, tmp_path)
    engine.QUEUE.mkdir()
    engine.SPOKEN.mkdir()
    waiting = engine.QUEUE / "004-af_heart-say.txt"
    earlier = engine.SPOKEN / "002-bm_fable-say.txt"
    waiting.write_text("Waiting", encoding="utf-8")
    earlier.write_text("Earlier", encoding="utf-8")
    engine.QUEUE_ORDER.write_text(
        json.dumps({"version": 1, "ids": [waiting.stem]}), encoding="utf-8"
    )
    engine.HISTORY_ORDER.write_text(
        json.dumps({"version": 1, "ids": [earlier.stem]}), encoding="utf-8"
    )

    prepare(engine)

    canonical_queue = list(engine.QUEUE.glob("*.txt"))
    canonical_history = list(engine.SPOKEN.glob("*.txt"))
    assert len(canonical_queue) == len(canonical_history) == 1
    queue_name = SpeechicleFilename.parse(canonical_queue[0].name)
    history_name = SpeechicleFilename.parse(canonical_history[0].name)
    assert read_ids(engine.QUEUE_ORDER) == [queue_name.public_id]
    assert read_ids(engine.HISTORY_ORDER) == [history_name.public_id]
    assert not engine.IDENTITY_INDEX.exists()
    assert not engine.TIMELINE_INTENT.exists()
