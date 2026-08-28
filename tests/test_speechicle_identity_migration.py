from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from speechicle_identity import (
    IdentityCatalog,
    SpeechicleFilename,
    embed_public_ids_from_intent,
    plan_embed_public_ids,
    write_catalog,
)


def public_id(digit: str) -> str:
    return f"sp_{digit * 32}"


def generated_ids(*digits: str) -> Callable[[], str]:
    values: Iterator[str] = iter(public_id(digit) for digit in digits)
    return lambda: next(values)


def migration_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    queue = tmp_path / "queue"
    spoken = tmp_path / "spoken"
    failed = tmp_path / "failed"
    for directory in (queue, spoken, failed):
        directory.mkdir()
    return (
        queue,
        spoken,
        failed,
        tmp_path / "queue-order.json",
        tmp_path / "history-order.json",
        tmp_path / "speechicle-index.json",
    )


def write_order(path: Path, version: int, ids: list[str]) -> None:
    path.write_text(json.dumps({"version": version, "ids": ids}), encoding="utf-8")


def plan(
    paths: tuple[Path, Path, Path, Path, Path, Path],
    *,
    generate: Callable[[], str] | None = None,
):
    return plan_embed_public_ids(*paths, "af_bella", generate=generate)


def test_precatalog_plan_preserves_suffix_metadata_and_separates_sequences(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, _ = paths
    suffixed = queue / "004old-af_heart-g250-say.txt"
    collision = spoken / "004-bm_fable-say.txt"
    malformed = spoken / "not-a-sequence-af_bella-g0-say.txt"
    suffixed.write_bytes(b"suffixed")
    collision.write_bytes(b"collision")
    malformed.write_bytes(b"malformed")
    write_order(queue_order, 1, [suffixed.stem])
    write_order(history_order, 1, [malformed.stem, collision.stem])

    migration = plan(paths, generate=generated_ids("1", "2", "3"))

    assert migration is not None
    assert embed_public_ids_from_intent(migration.intent_payload()) == migration
    by_source = {(item.source_root, item.source_name): item for item in migration.files}
    suffixed_target = SpeechicleFilename.parse(
        by_source[("queue", suffixed.name)].target_name
    )
    collision_target = SpeechicleFilename.parse(
        by_source[("spoken", collision.name)].target_name
    )
    malformed_target = SpeechicleFilename.parse(
        by_source[("spoken", malformed.name)].target_name
    )
    assert (suffixed_target.sequence, suffixed_target.voice, suffixed_target.gap_ms) == (
        4,
        "af_heart",
        250,
    )
    assert collision_target.sequence != suffixed_target.sequence
    assert collision_target.voice == "bm_fable"
    assert (malformed_target.voice, malformed_target.gap_ms) == ("af_bella", 0)
    assert {item.source_name for item in migration.history_files} == {
        malformed.name,
        collision.name,
    }
    assert suffixed.read_bytes() == b"suffixed"
    assert collision.read_bytes() == b"collision"
    assert malformed.read_bytes() == b"malformed"


def test_legacy_sequence_does_not_depend_on_voice_tail_and_voice_is_case_insensitive(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, _ = paths
    plain = queue / "004-note.txt"
    uppercase_voice = spoken / "005-AF_HEART-g250-say.txt"
    plain.write_text("plain", encoding="utf-8")
    uppercase_voice.write_text("uppercase", encoding="utf-8")
    write_order(queue_order, 1, [plain.stem])
    write_order(history_order, 1, [uppercase_voice.stem])

    migration = plan(paths, generate=generated_ids("4", "5"))

    assert migration is not None
    queue_target = SpeechicleFilename.parse(migration.queue_files[0].target_name)
    history_target = SpeechicleFilename.parse(migration.history_files[0].target_name)
    assert (queue_target.sequence, queue_target.voice, queue_target.gap_ms) == (
        4,
        "af_bella",
        None,
    )
    assert (history_target.sequence, history_target.voice, history_target.gap_ms) == (
        5,
        "af_heart",
        250,
    )


def test_missing_history_order_keeps_numbered_rows_ahead_of_malformed_rows(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    _, spoken, _, queue_order, _, _ = paths
    numbered = spoken / "002-af_heart-say.txt"
    malformed = spoken / "unknown-bm_fable-say.txt"
    numbered.write_text("numbered", encoding="utf-8")
    malformed.write_text("malformed", encoding="utf-8")
    write_order(queue_order, 1, [])

    migration = plan(paths, generate=generated_ids("2", "3"))

    assert migration is not None
    id_by_source = {
        item.source_name: SpeechicleFilename.parse(item.target_name).public_id
        for item in migration.files
    }
    assert migration.history_ids == (
        id_by_source[numbered.name],
        id_by_source[malformed.name],
    )


def test_current_catalog_and_v2_order_ids_survive_embedding(tmp_path: Path) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, catalog_path = paths
    queued = queue / "001-af_heart-say.txt"
    archived = spoken / "002-bm_fable-g100-say.txt"
    queued.write_text("queued", encoding="utf-8")
    archived.write_text("archived", encoding="utf-8")
    write_catalog(
        catalog_path,
        IdentityCatalog(3, {1: public_id("1"), 2: public_id("2")}),
    )
    write_order(queue_order, 2, [public_id("1")])
    write_order(history_order, 2, [public_id("2")])

    migration = plan(paths)

    assert migration is not None
    assert migration.queue_ids == (public_id("1"),)
    assert migration.history_ids == (public_id("2"),)
    filenames = [SpeechicleFilename.parse(item.target_name) for item in migration.files]
    assert {filename.public_id for filename in filenames} == {
        public_id("1"),
        public_id("2"),
    }


def test_v3_journal_does_not_preserve_the_retired_sequence_high_water(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, _, _, queue_order, history_order, catalog_path = paths
    queued = queue / "001-af_heart-say.txt"
    queued.write_text("queued", encoding="utf-8")
    write_catalog(catalog_path, IdentityCatalog(100, {1: public_id("1")}))
    write_order(queue_order, 2, [public_id("1")])
    write_order(history_order, 2, [])

    migration = plan(paths)

    assert migration is not None
    assert SpeechicleFilename.parse(migration.files[0].target_name).sequence == 1
    assert "next_sequence" not in migration.intent_payload()
    assert "catalog" not in migration.intent_payload()


def test_failed_speech_is_inventoried_but_excluded_from_timeline_orders(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, _, failed, queue_order, history_order, _ = paths
    queued = queue / "001-af_heart-say.txt"
    failed_row = failed / "002-bm_fable-g1500-say.txt"
    queued.write_text("queued", encoding="utf-8")
    failed_row.write_text("failed", encoding="utf-8")
    write_order(queue_order, 1, [queued.stem])
    write_order(history_order, 1, [])

    migration = plan(paths, generate=generated_ids("1", "2"))

    assert migration is not None
    assert len(migration.failed_files) == 1
    failed_entry = migration.failed_files[0]
    failed_filename = SpeechicleFilename.parse(failed_entry.target_name)
    assert failed_entry.source_root == "failed"
    assert failed_filename.gap_ms == 1500
    assert failed_filename.public_id not in {
        *migration.queue_ids,
        *migration.history_ids,
    }


def test_repairs_no_intent_partial_history_boundary(tmp_path: Path) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, _ = paths
    files = {
        6: queue / "006-af_heart-say.txt",
        3: queue / "003-af_heart-say.txt",
        2: queue / "002-af_heart-say.txt",
        1: spoken / "001-af_heart-say.txt",
    }
    for sequence, path in files.items():
        path.write_text(str(sequence), encoding="utf-8")
    write_order(queue_order, 1, [files[6].stem])
    write_order(
        history_order,
        1,
        [files[3].stem, files[2].stem, files[1].stem],
    )

    migration = plan(paths, generate=generated_ids("2", "3", "6", "1"))

    assert migration is not None
    assert migration.queue_ids == (
        public_id("2"),
        public_id("3"),
        public_id("6"),
    )
    assert migration.history_ids == (public_id("1"),)
    target_by_id = {
        filename.public_id: filename
        for filename in (
            SpeechicleFilename.parse(item.target_name) for item in migration.files
        )
    }
    assert [target_by_id[item].sequence for item in migration.queue_ids] == [2, 3, 6]
    assert [target_by_id[item].sequence for item in migration.history_ids] == [1]
    assert all(item.source_root == "queue" for item in migration.queue_files)
    assert all(item.source_root == "spoken" for item in migration.history_files)


def test_mixed_canonical_and_legacy_layout_fails_even_with_a_catalog(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, catalog_path = paths
    canonical = SpeechicleFilename(1, public_id("1"), "af_heart")
    (queue / canonical.render()).write_text("canonical", encoding="utf-8")
    (spoken / "002-bm_fable-say.txt").write_text("legacy", encoding="utf-8")
    write_catalog(
        catalog_path,
        IdentityCatalog(3, {1: public_id("1"), 2: public_id("2")}),
    )
    write_order(queue_order, 2, [public_id("1")])
    write_order(history_order, 2, [public_id("2")])

    with pytest.raises(ValueError, match="mixed legacy and canonical"):
        plan(paths)


def test_duplicate_sequence_uses_v2_order_to_keep_the_existing_id(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, catalog_path = paths
    queued = queue / "004-af_heart-say.txt"
    archived = spoken / "004-bm_fable-say.txt"
    queued.write_text("queued", encoding="utf-8")
    archived.write_text("archived", encoding="utf-8")
    write_catalog(catalog_path, IdentityCatalog(5, {4: public_id("4")}))
    write_order(queue_order, 2, [public_id("4")])
    write_order(history_order, 2, [])

    migration = plan(paths, generate=generated_ids("5"))

    assert migration is not None
    by_source = {item.source_name: item for item in migration.files}
    assert SpeechicleFilename.parse(by_source[queued.name].target_name).public_id == public_id(
        "4"
    )
    replacement = SpeechicleFilename.parse(by_source[archived.name].target_name)
    assert replacement.public_id == public_id("5")
    assert replacement.sequence > 4


def test_duplicate_catalog_sequence_without_v2_ownership_fails(tmp_path: Path) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, catalog_path = paths
    (queue / "004-af_heart-say.txt").write_text("queued", encoding="utf-8")
    (spoken / "004-bm_fable-say.txt").write_text("archived", encoding="utf-8")
    write_catalog(catalog_path, IdentityCatalog(5, {4: public_id("4")}))
    write_order(queue_order, 1, ["004-af_heart-say"])
    write_order(history_order, 1, ["004-bm_fable-say"])

    with pytest.raises(ValueError, match="prove duplicate sequence ownership"):
        plan(paths)


def test_catalog_backed_layout_rejects_an_unmapped_legacy_sequence(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, _, _, queue_order, history_order, catalog_path = paths
    orphan = queue / "002-af_heart-say.txt"
    orphan.write_text("orphan", encoding="utf-8")
    write_catalog(catalog_path, IdentityCatalog(3, {1: public_id("1")}))
    write_order(queue_order, 1, [orphan.stem])
    write_order(history_order, 1, [])

    with pytest.raises(ValueError, match="no ID for sequence 2"):
        plan(paths)


def test_exact_replay_duplicate_is_removed_without_losing_promoted_rows(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, _ = paths
    active = queue / "003-af_heart-say.txt"
    duplicate = spoken / active.name
    promoted = spoken / "004-bm_fable-say.txt"
    older = spoken / "001-af_bella-say.txt"
    active.write_bytes(b"same")
    duplicate.write_bytes(b"same")
    promoted.write_bytes(b"promoted")
    older.write_bytes(b"older")
    write_order(queue_order, 1, [active.stem])
    write_order(history_order, 1, [promoted.stem, duplicate.stem, older.stem])

    migration = plan(paths, generate=generated_ids("1", "2", "3"))

    assert migration is not None
    assert len(migration.removals) == 1
    assert migration.removals[0].source_name == duplicate.name
    by_source = {(item.source_root, item.source_name): item for item in migration.files}
    assert any(
        item.source_root == "spoken" and item.source_name == promoted.name
        for item in migration.queue_files
    )
    assert ("spoken", duplicate.name) not in by_source
    assert len(migration.files) == 3


def test_same_named_distinct_rows_are_never_reconciled_as_replay_duplicates(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, _ = paths
    name = "003-af_heart-say.txt"
    (queue / name).write_bytes(b"queue")
    (spoken / name).write_bytes(b"spoken")
    write_order(queue_order, 1, [Path(name).stem])
    write_order(history_order, 1, [Path(name).stem])

    migration = plan(paths, generate=generated_ids("1", "2"))

    assert migration is not None
    assert migration.removals == ()
    assert {(item.source_root, item.source_name) for item in migration.files} == {
        ("queue", name),
        ("spoken", name),
    }
    assert len({item.target_name for item in migration.files}) == 2


def test_canonical_only_layout_ignores_a_stale_corrupt_catalog(tmp_path: Path) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, catalog_path = paths
    queued = SpeechicleFilename(1, public_id("1"), "af_heart")
    archived = SpeechicleFilename(2, public_id("2"), "bm_fable")
    (queue / queued.render()).write_text("queued", encoding="utf-8")
    (spoken / archived.render()).write_text("archived", encoding="utf-8")
    write_order(queue_order, 2, [queued.public_id])
    write_order(history_order, 2, [archived.public_id])
    catalog_path.write_text("{broken", encoding="utf-8")

    assert plan(paths) is None


def test_catalogless_generation_is_blocked_by_v2_orders_and_mutations(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, _, _, queue_order, history_order, _ = paths
    queued = queue / "001-af_heart-say.txt"
    queued.write_text("queued", encoding="utf-8")
    write_order(queue_order, 2, [public_id("1")])
    write_order(history_order, 2, [])
    with pytest.raises(ValueError, match="requires the identity catalog"):
        plan(paths)

    write_order(queue_order, 1, [queued.stem])
    write_order(history_order, 1, [])
    (tmp_path / f"MUTATION.s{'0' * 20}.{'a' * 24}.claim").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="durable mutation"):
        plan(paths)


def test_timeline_intent_blocks_planning_for_every_layout(tmp_path: Path) -> None:
    paths = migration_paths(tmp_path)
    (tmp_path / "timeline-intent.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="timeline-intent.json"):
        plan(paths)


@pytest.mark.parametrize("section", ["queue", "history"])
def test_unknown_v1_order_entry_fails_closed(tmp_path: Path, section: str) -> None:
    paths = migration_paths(tmp_path)
    queue, spoken, _, queue_order, history_order, _ = paths
    directory = queue if section == "queue" else spoken
    (directory / "001-af_heart-say.txt").write_text("speech", encoding="utf-8")
    write_order(queue_order, 1, ["missing"] if section == "queue" else [])
    write_order(history_order, 1, ["missing"] if section == "history" else [])

    with pytest.raises(ValueError, match="unknown or ambiguous legacy row"):
        plan(paths)


def test_ambiguous_v1_sequence_fallback_fails_closed(tmp_path: Path) -> None:
    paths = migration_paths(tmp_path)
    queue, _, _, queue_order, history_order, _ = paths
    (queue / "004a-af_heart-say.txt").write_text("first", encoding="utf-8")
    (queue / "004b-bm_fable-say.txt").write_text("second", encoding="utf-8")
    write_order(queue_order, 1, ["004-old-name"])
    write_order(history_order, 1, [])

    with pytest.raises(ValueError, match="unknown or ambiguous legacy row"):
        plan(paths)


@pytest.mark.parametrize(
    "saved_stem",
    [
        "folder/name",
        "folder\\name",
        "bad:name",
        "bad\0name",
        "bad.",
        "bad ",
        "NUL",
        "aux.data",
        "COM1",
        "lpt9.data",
    ],
)
def test_v1_order_rejects_windows_unsafe_stems(
    tmp_path: Path,
    saved_stem: str,
) -> None:
    paths = migration_paths(tmp_path)
    _, _, _, queue_order, history_order, _ = paths
    write_order(queue_order, 1, [saved_stem])
    write_order(history_order, 1, [])

    with pytest.raises(ValueError, match="invalid speech order"):
        plan(paths)


def test_v2_order_rejects_an_unknown_public_id_even_with_a_catalog(
    tmp_path: Path,
) -> None:
    paths = migration_paths(tmp_path)
    queue, _, _, queue_order, history_order, catalog_path = paths
    (queue / "001-af_heart-say.txt").write_text("queued", encoding="utf-8")
    write_catalog(catalog_path, IdentityCatalog(2, {1: public_id("1")}))
    write_order(queue_order, 2, [public_id("9")])
    write_order(history_order, 2, [])

    with pytest.raises(ValueError, match="unknown public ID"):
        plan(paths)


def test_corrupt_catalog_with_v2_orders_fails_closed(tmp_path: Path) -> None:
    paths = migration_paths(tmp_path)
    queue, _, _, queue_order, history_order, catalog_path = paths
    (queue / "001-af_heart-say.txt").write_text("queued", encoding="utf-8")
    catalog_path.write_text("{broken", encoding="utf-8")
    write_order(queue_order, 2, [public_id("1")])
    write_order(history_order, 2, [])

    with pytest.raises(ValueError, match="identity catalog"):
        plan(paths)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 3, "ids": []},
        {"version": 1.0, "ids": []},
        {"version": 2, "ids": ["not-a-public-id"]},
        {"version": 1, "ids": ["../outside"]},
        {"version": 1, "ids": [], "extra": True},
    ],
)
def test_present_malformed_or_unknown_order_sidecar_fails(
    tmp_path: Path,
    payload: object,
) -> None:
    paths = migration_paths(tmp_path)
    _, _, _, queue_order, history_order, _ = paths
    queue_order.write_text(json.dumps(payload), encoding="utf-8")
    write_order(history_order, 1, [])

    with pytest.raises(ValueError, match="speech order"):
        plan(paths)
