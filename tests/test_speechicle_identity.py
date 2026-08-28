from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from speechicle_identity import (
    EmbedPublicIdsFile,
    EmbedPublicIdsMigration,
    ExactReplayDuplicateRemoval,
    IdentityCatalog,
    SpeechicleFilename,
    allocate_identity,
    catalog_from_payload,
    catalog_payload,
    embed_public_ids_from_intent,
    generate_public_id,
    is_public_id,
    load_catalog,
    migration_from_intent,
    plan_identity_migration,
    write_catalog,
)


def test_canonical_filename_round_trip_and_voice_change() -> None:
    public_id = f"sp_{'a' * 32}"
    filename = SpeechicleFilename(7, public_id, "af_heart", 250)

    assert filename.render() == f"007-{public_id}-af_heart-g250-say.txt"
    assert SpeechicleFilename.parse(filename.render()) == filename
    assert filename.with_voice("bm_fable") == SpeechicleFilename(
        7, public_id, "bm_fable", 250
    )
    assert filename == SpeechicleFilename(7, public_id, "af_heart", 250)


@pytest.mark.parametrize(
    "name",
    [
        f"07-sp_{'a' * 32}-af_heart-say.txt",
        f"0007-sp_{'a' * 32}-af_heart-say.txt",
        f"007-sp_{'A' * 32}-af_heart-say.txt",
        f"007-sp_{'a' * 32}-AF_heart-say.txt",
        f"007-sp_{'a' * 32}-af_heart-g0250-say.txt",
        f"007-sp_{'a' * 32}-af_heart-g1501-say.txt",
        f"007-sp_{'a' * 32}-af_heart.txt",
    ],
)
def test_canonical_filename_parser_rejects_noncanonical_variants(name: str) -> None:
    with pytest.raises(ValueError, match="canonical|gap"):
        SpeechicleFilename.parse(name)


def test_public_id_generation_retries_invalid_existing_and_duplicate_values() -> None:
    existing = f"sp_{'a' * 32}"
    generated = iter(["bad", existing, f"sp_{'b' * 32}"])

    assert generate_public_id({existing}, generate=lambda: next(generated)) == f"sp_{'b' * 32}"


def test_embed_public_ids_journal_round_trip_is_self_contained() -> None:
    first_id = f"sp_{'1' * 32}"
    second_id = f"sp_{'2' * 32}"
    digest = "a" * 64
    migration = EmbedPublicIdsMigration(
        (
            EmbedPublicIdsFile(
                "queue",
                "001-af_heart-say.txt",
                SpeechicleFilename(1, first_id, "af_heart").render(),
                digest,
            ),
        ),
        (
            EmbedPublicIdsFile(
                "spoken",
                "002-bm_fable-say.txt",
                SpeechicleFilename(2, second_id, "bm_fable", 100).render(),
                "b" * 64,
            ),
        ),
        (),
        (
            ExactReplayDuplicateRemoval(
                "spoken",
                "001-af_heart-say.txt",
                "queue",
                "001-af_heart-say.txt",
                digest,
            ),
        ),
    )

    payload = migration.intent_payload()
    assert set(payload) == {
        "version",
        "operation",
        "queue_files",
        "history_files",
        "failed_files",
        "removals",
    }
    assert set(payload["queue_files"][0]) == {
        "source_root",
        "source_name",
        "target_name",
        "sha256",
    }
    assert embed_public_ids_from_intent(payload) == migration
    assert migration.queue_ids == (first_id,)
    assert migration.history_ids == (second_id,)


def test_embed_public_ids_parser_rejects_target_collisions() -> None:
    public_id = f"sp_{'1' * 32}"
    target = SpeechicleFilename(1, public_id, "af_heart").render()
    migration = EmbedPublicIdsMigration(
        (
            EmbedPublicIdsFile("queue", "one.txt", target, "a" * 64),
            EmbedPublicIdsFile("spoken", "two.txt", target, "b" * 64),
        ),
        (),
        (),
        (),
    )
    with pytest.raises(ValueError, match="collision"):
        embed_public_ids_from_intent(migration.intent_payload())


def test_embed_public_ids_parser_rejects_target_source_ambiguity() -> None:
    first_id = f"sp_{'1' * 32}"
    second_id = f"sp_{'2' * 32}"
    first_target = SpeechicleFilename(1, first_id, "af_heart").render()
    second_target = SpeechicleFilename(2, second_id, "af_heart").render()
    migration = EmbedPublicIdsMigration(
        (
            EmbedPublicIdsFile("queue", "legacy.txt", first_target, "a" * 64),
            EmbedPublicIdsFile("queue", first_target, second_target, "b" * 64),
        ),
        (),
        (),
        (),
    )

    with pytest.raises(ValueError, match="source"):
        embed_public_ids_from_intent(migration.intent_payload())


def test_embed_public_ids_parser_rejects_duplicate_target_sequences() -> None:
    first_id = f"sp_{'1' * 32}"
    second_id = f"sp_{'2' * 32}"
    migration = EmbedPublicIdsMigration(
        (
            EmbedPublicIdsFile(
                "queue",
                "one.txt",
                SpeechicleFilename(1, first_id, "af_heart").render(),
                "a" * 64,
            ),
        ),
        (
            EmbedPublicIdsFile(
                "spoken",
                "two.txt",
                SpeechicleFilename(1, second_id, "bm_fable").render(),
                "b" * 64,
            ),
        ),
        (),
        (),
    )

    with pytest.raises(ValueError, match="identities"):
        embed_public_ids_from_intent(migration.intent_payload())


@pytest.mark.parametrize("version", [3.0, True, 2, "3"])
def test_embed_public_ids_parser_requires_exact_integer_version(version: object) -> None:
    with pytest.raises(ValueError, match="invalid pending"):
        embed_public_ids_from_intent(
            {
                "version": version,
                "operation": "embed_public_ids",
                "queue_files": [],
                "history_files": [],
                "failed_files": [],
                "removals": [],
            }
        )


@pytest.mark.parametrize(
    ("target_collection", "source_root"),
    [
        ("history", "queue"),
        ("history", "failed"),
        ("queue", "failed"),
        ("failed", "queue"),
        ("failed", "spoken"),
    ],
)
def test_embed_public_ids_parser_rejects_unsupported_root_transitions(
    target_collection: str,
    source_root: str,
) -> None:
    target = SpeechicleFilename(1, f"sp_{'1' * 32}", "af_heart").render()
    files = (EmbedPublicIdsFile(source_root, "source.txt", target, "a" * 64),)
    migration = EmbedPublicIdsMigration(
        files if target_collection == "queue" else (),
        files if target_collection == "history" else (),
        files if target_collection == "failed" else (),
        (),
    )

    with pytest.raises(ValueError, match="invalid file entry"):
        embed_public_ids_from_intent(migration.intent_payload())


@pytest.mark.parametrize(
    "source_name",
    [
        "folder/name.txt",
        "folder\\name.txt",
        "bad:name.txt",
        "bad\0name.txt",
        "bad.txt.",
        "bad.txt ",
        "NUL.txt",
        "con.log.txt",
        "CONIN$.txt",
        "conout$.log.txt",
        "COM1.txt",
        "lpt9.data.txt",
    ],
)
def test_embed_public_ids_parser_rejects_windows_unsafe_names(
    source_name: str,
) -> None:
    target = SpeechicleFilename(1, f"sp_{'1' * 32}", "af_heart").render()
    migration = EmbedPublicIdsMigration(
        (EmbedPublicIdsFile("queue", source_name, target, "a" * 64),),
        (),
        (),
        (),
    )

    with pytest.raises(ValueError, match="invalid file entry"):
        embed_public_ids_from_intent(migration.intent_payload())


def test_embed_public_ids_parser_rejects_windows_unsafe_removal_names() -> None:
    target = SpeechicleFilename(1, f"sp_{'1' * 32}", "af_heart").render()
    digest = "a" * 64
    migration = EmbedPublicIdsMigration(
        (EmbedPublicIdsFile("queue", "source.txt", target, digest),),
        (),
        (),
        (
            ExactReplayDuplicateRemoval(
                "spoken", "NUL.txt", "queue", "source.txt", digest
            ),
        ),
    )

    with pytest.raises(ValueError, match="invalid replay removal"):
        embed_public_ids_from_intent(migration.intent_payload())


def test_embed_public_ids_parser_rejects_inverse_replay_removal() -> None:
    target = SpeechicleFilename(1, f"sp_{'1' * 32}", "af_heart").render()
    digest = "a" * 64
    migration = EmbedPublicIdsMigration(
        (),
        (EmbedPublicIdsFile("spoken", "source.txt", target, digest),),
        (),
        (
            ExactReplayDuplicateRemoval(
                "queue", "source.txt", "spoken", "source.txt", digest
            ),
        ),
    )

    with pytest.raises(ValueError, match="invalid replay removal"):
        embed_public_ids_from_intent(migration.intent_payload())


def test_embed_public_ids_parser_rejects_multiple_replay_removals() -> None:
    target = SpeechicleFilename(1, f"sp_{'1' * 32}", "af_heart").render()
    digest = "a" * 64
    migration = EmbedPublicIdsMigration(
        (EmbedPublicIdsFile("queue", "source.txt", target, digest),),
        (),
        (),
        (
            ExactReplayDuplicateRemoval(
                "spoken", "source.txt", "queue", "source.txt", digest
            ),
            ExactReplayDuplicateRemoval(
                "spoken", "other.txt", "queue", "other.txt", digest
            ),
        ),
    )

    with pytest.raises(ValueError, match="multiple replay removals"):
        embed_public_ids_from_intent(migration.intent_payload())


def test_catalog_round_trip_keeps_retired_sequences(tmp_path: Path) -> None:
    catalog = IdentityCatalog(
        8,
        {
            2: f"sp_{'2' * 32}",
            7: f"sp_{'7' * 32}",
        },
    )
    path = tmp_path / "speechicle-index.json"

    write_catalog(path, catalog)

    assert load_catalog(path) == catalog
    assert json.loads(path.read_text(encoding="utf-8")) == catalog_payload(catalog)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "next_sequence": 1, "ids_by_sequence": {}},
        {"version": 1, "next_sequence": 1, "ids_by_sequence": {"0": f"sp_{'1' * 32}"}},
        {"version": 1, "next_sequence": 1, "ids_by_sequence": {"1": f"sp_{'1' * 32}"}},
        {
            "version": 1,
            "next_sequence": 3,
            "ids_by_sequence": {
                "1": f"sp_{'1' * 32}",
                "2": f"sp_{'1' * 32}",
            },
        },
        {"version": 1, "next_sequence": 2, "ids_by_sequence": {"1": "speech-1"}},
    ],
)
def test_catalog_validation_rejects_ambiguous_or_reusable_identity(
    payload: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        catalog_from_payload(payload)


def test_allocation_advances_past_retired_sequences_and_retries_bad_ids() -> None:
    retired_id = f"sp_{'a' * 32}"
    generated = iter([retired_id, "not-an-id", f"sp_{'b' * 32}"])
    catalog = IdentityCatalog(12, {11: retired_id})

    updated, sequence, public_id = allocate_identity(
        catalog, generate=lambda: next(generated)
    )

    assert sequence == 12
    assert public_id == f"sp_{'b' * 32}"
    assert updated.next_sequence == 13
    assert updated.ids_by_sequence == {11: retired_id, 12: public_id}


def test_migration_preserves_order_and_separates_colliding_storage_sequences(
    tmp_path: Path,
) -> None:
    queue = tmp_path / "queue"
    spoken = tmp_path / "spoken"
    failed = tmp_path / "failed"
    for directory in (queue, spoken, failed):
        directory.mkdir()
    queued = queue / "004-af_heart-say.txt"
    colliding = spoken / "004-bm_fable-say.txt"
    malformed = spoken / "4oops-af_bella-say.txt"
    queued.write_text("Queued", encoding="utf-8")
    colliding.write_text("Colliding", encoding="utf-8")
    malformed.write_text("Malformed", encoding="utf-8")
    queue_order = tmp_path / "queue-order.json"
    history_order = tmp_path / "history-order.json"
    queue_order.write_text(
        json.dumps({"version": 1, "ids": [queued.stem]}), encoding="utf-8"
    )
    history_order.write_text(
        json.dumps({"version": 1, "ids": [malformed.stem, colliding.stem]}),
        encoding="utf-8",
    )

    migration = plan_identity_migration(
        queue,
        spoken,
        failed,
        queue_order,
        history_order,
        None,
        "af_bella",
    )

    assert migration is not None
    assert len({*migration.queue_ids, *migration.history_ids}) == 3
    assert all(is_public_id(item) for item in migration.queue_ids)
    assert all(is_public_id(item) for item in migration.history_ids)
    assert migration.catalog.next_sequence > 4
    assert len(migration.moves) == 2
    recovered = migration_from_intent(migration.intent_payload())
    assert recovered.catalog == migration.catalog
    assert recovered.moves == migration.moves
    assert recovered.removals == migration.removals
    assert recovered.queue_ids == migration.queue_ids
    assert recovered.history_ids == migration.history_ids
