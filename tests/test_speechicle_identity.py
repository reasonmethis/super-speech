from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from speechicle_identity import (
    IdentityCatalog,
    allocate_identity,
    catalog_from_payload,
    catalog_payload,
    is_public_id,
    load_catalog,
    migration_from_intent,
    plan_identity_migration,
    write_catalog,
)


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
