from __future__ import annotations

import json
from pathlib import Path

import pytest

from speechicle_identity import (
    EmbedPublicIdsFile,
    EmbedPublicIdsMigration,
    ExactReplayDuplicateRemoval,
    IdentityCatalog,
    SpeechicleFilename,
    catalog_from_payload,
    catalog_payload,
    embed_public_ids_from_intent,
    generate_public_id,
    load_catalog,
    write_catalog,
)


def public_id(digit: str) -> str:
    return f"sp_{digit * 32}"


def test_canonical_filename_round_trip_and_voice_change() -> None:
    speechicle_id = public_id("a")
    filename = SpeechicleFilename(7, speechicle_id, "af_heart", 250)

    assert filename.render() == f"007-{speechicle_id}-af_heart-g250-say.txt"
    assert SpeechicleFilename.parse(filename.render()) == filename
    assert filename.with_voice("bm_fable") == SpeechicleFilename(
        7, speechicle_id, "bm_fable", 250
    )


@pytest.mark.parametrize(
    "name",
    [
        f"07-{public_id('a')}-af_heart-say.txt",
        f"0007-{public_id('a')}-af_heart-say.txt",
        f"007-{public_id('A')}-af_heart-say.txt",
        f"007-{public_id('a')}-AF_heart-say.txt",
        f"007-{public_id('a')}-af_heart-g0250-say.txt",
        f"007-{public_id('a')}-af_heart-g1501-say.txt",
        f"007-{public_id('a')}-af_heart.txt",
    ],
)
def test_canonical_filename_parser_rejects_noncanonical_variants(name: str) -> None:
    with pytest.raises(ValueError, match="canonical|gap"):
        SpeechicleFilename.parse(name)


def test_public_id_generation_retries_invalid_existing_and_duplicate_values() -> None:
    existing = public_id("a")
    generated = iter(["bad", existing, public_id("b")])

    assert generate_public_id(
        {existing}, generate=lambda: next(generated)
    ) == public_id("b")


def test_embed_public_ids_journal_round_trip_is_self_contained() -> None:
    first_id = public_id("1")
    second_id = public_id("2")
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
    target = SpeechicleFilename(1, public_id("1"), "af_heart").render()
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
    first_id = public_id("1")
    second_id = public_id("2")
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
    first_id = public_id("1")
    second_id = public_id("2")
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
    target = SpeechicleFilename(1, public_id("1"), "af_heart").render()
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
    target = SpeechicleFilename(1, public_id("1"), "af_heart").render()
    migration = EmbedPublicIdsMigration(
        (EmbedPublicIdsFile("queue", source_name, target, "a" * 64),),
        (),
        (),
        (),
    )

    with pytest.raises(ValueError, match="invalid file entry"):
        embed_public_ids_from_intent(migration.intent_payload())


def test_embed_public_ids_parser_rejects_windows_unsafe_removal_names() -> None:
    target = SpeechicleFilename(1, public_id("1"), "af_heart").render()
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
    target = SpeechicleFilename(1, public_id("1"), "af_heart").render()
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
    target = SpeechicleFilename(1, public_id("1"), "af_heart").render()
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


def test_legacy_catalog_round_trip_preserves_sparse_identity_map(
    tmp_path: Path,
) -> None:
    catalog = IdentityCatalog(
        8,
        {
            2: public_id("2"),
            7: public_id("7"),
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
        {"version": 1, "next_sequence": 1, "ids_by_sequence": {"0": public_id("1")}},
        {"version": 1, "next_sequence": 1, "ids_by_sequence": {"1": public_id("1")}},
        {
            "version": 1,
            "next_sequence": 3,
            "ids_by_sequence": {
                "1": public_id("1"),
                "2": public_id("1"),
            },
        },
        {"version": 1, "next_sequence": 2, "ids_by_sequence": {"1": "speech-1"}},
    ],
)
def test_legacy_catalog_validation_rejects_invalid_or_reusable_identity(
    payload: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        catalog_from_payload(payload)
