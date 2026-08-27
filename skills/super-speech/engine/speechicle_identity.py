"""Persistent public identities for speech files.

The engine keeps numeric filename prefixes for cheap local storage ordering. Those
numbers are private implementation details. This module owns the public IDs that
stay attached to a Speechicle when its file moves or its metadata changes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PUBLIC_ID_PATTERN = re.compile(r"sp_[a-f0-9]{32}\Z")
STRICT_SEQUENCE_PATTERN = re.compile(r"([0-9]+)-.+\.txt\Z")
CATALOG_VERSION = 1


@dataclass(frozen=True)
class IdentityCatalog:
    """The durable mapping from private storage sequences to public IDs."""

    next_sequence: int
    ids_by_sequence: dict[int, str]

    def public_id(self, sequence: int) -> str | None:
        return self.ids_by_sequence.get(sequence)


@dataclass(frozen=True)
class MigrationMove:
    source_directory: str
    target_directory: str
    source: str
    target: str


@dataclass(frozen=True)
class MigrationRemoval:
    directory: str
    name: str


@dataclass(frozen=True)
class IdentityMigration:
    catalog: IdentityCatalog
    moves: tuple[MigrationMove, ...]
    removals: tuple[MigrationRemoval, ...]
    queue_ids: tuple[str, ...]
    history_ids: tuple[str, ...]
    row_count: int = 0

    def intent_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "operation": "identity_migration",
            "moves": [move.__dict__ for move in self.moves],
            "removals": [removal.__dict__ for removal in self.removals],
            "catalog": catalog_payload(self.catalog),
            "queue_ids": list(self.queue_ids),
            "history_ids": list(self.history_ids),
        }


def is_public_id(value: object) -> bool:
    return isinstance(value, str) and PUBLIC_ID_PATTERN.fullmatch(value) is not None


def strict_sequence(filename: str) -> int | None:
    """Return a positive numeric prefix only for a well-formed storage filename."""
    match = STRICT_SEQUENCE_PATTERN.fullmatch(filename)
    if match is None:
        return None
    sequence = int(match.group(1))
    return sequence if sequence > 0 else None


def catalog_from_payload(payload: object) -> IdentityCatalog:
    """Validate and normalize one catalog JSON value."""
    if not isinstance(payload, dict) or payload.get("version") != CATALOG_VERSION:
        raise ValueError("identity catalog has an unsupported version")
    next_sequence = payload.get("next_sequence")
    raw_ids = payload.get("ids_by_sequence")
    if not isinstance(next_sequence, int) or isinstance(next_sequence, bool):
        raise TypeError("identity catalog next_sequence must be an integer")
    if next_sequence < 1 or not isinstance(raw_ids, dict):
        raise ValueError("identity catalog has an invalid sequence range")

    ids_by_sequence: dict[int, str] = {}
    public_ids: set[str] = set()
    for raw_sequence, public_id in raw_ids.items():
        if not isinstance(raw_sequence, str) or not re.fullmatch(r"[1-9][0-9]*", raw_sequence):
            raise ValueError("identity catalog contains an invalid sequence")
        sequence = int(raw_sequence)
        if not is_public_id(public_id):
            raise ValueError("identity catalog contains an invalid public ID")
        if public_id in public_ids:
            raise ValueError("identity catalog contains a duplicate public ID")
        ids_by_sequence[sequence] = public_id
        public_ids.add(public_id)

    if ids_by_sequence and next_sequence <= max(ids_by_sequence):
        raise ValueError("identity catalog next_sequence must exceed every stored sequence")
    return IdentityCatalog(next_sequence, ids_by_sequence)


def catalog_payload(catalog: IdentityCatalog) -> dict[str, object]:
    return {
        "version": CATALOG_VERSION,
        "next_sequence": catalog.next_sequence,
        "ids_by_sequence": {
            str(sequence): public_id
            for sequence, public_id in sorted(catalog.ids_by_sequence.items())
        },
    }


def load_catalog(path: Path) -> IdentityCatalog | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read identity catalog: {error}") from error
    return catalog_from_payload(payload)


def write_catalog(path: Path, catalog: IdentityCatalog) -> None:
    """Atomically replace the catalog without reusing its temporary filename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(catalog_payload(catalog)), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def allocate_identity(
    catalog: IdentityCatalog,
    *,
    generate: Callable[[], str] | None = None,
) -> tuple[IdentityCatalog, int, str]:
    """Return a new catalog plus one never-reused sequence and public ID."""
    generate_id = generate or (lambda: f"sp_{secrets.token_hex(16)}")
    existing_ids = set(catalog.ids_by_sequence.values())
    public_id = generate_id()
    while not is_public_id(public_id) or public_id in existing_ids:
        public_id = generate_id()
    sequence = catalog.next_sequence
    ids_by_sequence = {**catalog.ids_by_sequence, sequence: public_id}
    return IdentityCatalog(sequence + 1, ids_by_sequence), sequence, public_id


def catalog_with_mapping(
    catalog: IdentityCatalog,
    sequence: int,
    public_id: str,
) -> IdentityCatalog:
    """Add one migration mapping while preserving the catalog high-water mark."""
    if sequence < 1 or not is_public_id(public_id):
        raise ValueError("invalid identity mapping")
    existing = catalog.ids_by_sequence.get(sequence)
    if existing is not None and existing != public_id:
        raise ValueError(f"sequence {sequence} already has another public ID")
    if public_id in catalog.ids_by_sequence.values() and existing != public_id:
        raise ValueError("public ID already belongs to another sequence")
    ids_by_sequence = {**catalog.ids_by_sequence, sequence: public_id}
    return IdentityCatalog(max(catalog.next_sequence, sequence + 1), ids_by_sequence)


def sequence_for_public_id(catalog: IdentityCatalog, public_id: str) -> int | None:
    if not is_public_id(public_id):
        return None
    return next(
        (
            sequence
            for sequence, existing_id in catalog.ids_by_sequence.items()
            if existing_id == public_id
        ),
        None,
    )


def catalog_seeded_above(sequences: list[int]) -> IdentityCatalog:
    """Start migration above every sequence ever observed in legacy storage."""
    return IdentityCatalog(max(sequences, default=0) + 1, {})


def legacy_sequence(filename: str) -> int | None:
    prefix = filename.split("-", 1)[0]
    match = re.match(r"[0-9]+", prefix)
    return int(match.group()) if match else None


def read_order_payload(path: Path) -> tuple[int, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 2, []
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read speech order: {path.name}") from error
    if not isinstance(payload, dict):
        return 1, []
    version = payload.get("version")
    raw_ids = payload.get("ids", [])
    if version not in {1, 2} or not (
        isinstance(raw_ids, list) and all(isinstance(item, str) for item in raw_ids)
    ):
        return 1, []
    return version, raw_ids


def paths_in_saved_order(
    directory: Path,
    order_path: Path,
    *,
    history: bool,
    catalog: IdentityCatalog | None,
) -> list[Path]:
    """Project a legacy or current sidecar onto the files that still exist."""
    version, saved_ids = read_order_payload(order_path)
    remaining = {path.name: path for path in directory.glob("*.txt")}
    ordered: list[Path] = []
    for saved_id in saved_ids:
        matched: Path | None = None
        if version == 1:
            matched = remaining.pop(f"{saved_id}.txt", None)
            if matched is None:
                saved_sequence = legacy_sequence(saved_id)
                variants = [
                    path
                    for path in remaining.values()
                    if legacy_sequence(path.name) == saved_sequence
                ]
                if saved_sequence is not None and len(variants) == 1:
                    matched = remaining.pop(variants[0].name)
        elif catalog is not None:
            saved_sequence = sequence_for_public_id(catalog, saved_id)
            variants = [
                path
                for path in remaining.values()
                if strict_sequence(path.name) == saved_sequence
            ]
            if saved_sequence is not None and len(variants) == 1:
                matched = remaining.pop(variants[0].name)
        if matched is not None:
            ordered.append(matched)

    def sort_key(path: Path) -> tuple[bool, int, str]:
        sequence = legacy_sequence(path.name)
        if history:
            return (sequence is not None, sequence or 0, path.name)
        return (sequence is None, sequence or 0, path.name)

    missing = sorted(remaining.values(), key=sort_key, reverse=history)
    return [*missing, *ordered] if history else [*ordered, *missing]


def _legacy_filename_tail(filename: str, default_voice: str) -> str:
    match = re.search(
        r"([ab][fm]_[a-z0-9_]+(?:-g[0-9]+)?-say)\.txt\Z",
        filename,
        re.IGNORECASE,
    )
    return f"{match.group(1).lower()}.txt" if match else f"{default_voice}-say.txt"


def _fresh_public_id(existing_ids: set[str]) -> str:
    public_id = f"sp_{secrets.token_hex(16)}"
    while public_id in existing_ids:
        public_id = f"sp_{secrets.token_hex(16)}"
    existing_ids.add(public_id)
    return public_id


def plan_identity_migration(
    queue_directory: Path,
    spoken_directory: Path,
    failed_directory: Path,
    queue_order_path: Path,
    history_order_path: Path,
    existing: IdentityCatalog | None,
    default_voice: str,
) -> IdentityMigration | None:
    """Build one deterministic, restart-safe migration from the current files."""
    queue_order = paths_in_saved_order(
        queue_directory, queue_order_path, history=False, catalog=existing
    )
    history_order = paths_in_saved_order(
        spoken_directory, history_order_path, history=True, catalog=existing
    )
    failed_order = sorted(
        failed_directory.glob("*.txt"),
        key=lambda path: (
            legacy_sequence(path.name) is None,
            legacy_sequence(path.name) or 0,
            path.name,
        ),
    )

    removals: list[MigrationRemoval] = []
    cross_directory_sources: dict[tuple[str, str], Path] = {}
    queue_by_name = {path.name: path for path in queue_order}
    exact_duplicates = [
        (index, archived, active)
        for index, archived in enumerate(history_order)
        if (active := queue_by_name.get(archived.name)) is not None
        and active.read_bytes() == archived.read_bytes()
    ]
    if len(exact_duplicates) == 1:
        selected_index, selected_history, _ = exact_duplicates[0]
        promoted = history_order[: selected_index + 1]
        targets = [queue_directory / path.name for path in promoted]
        target_names = {path.name for path in targets}
        conflicts = [
            target
            for source, target in zip(promoted, targets)
            if target.exists() and source != selected_history
        ]
        if not conflicts:
            removals.append(MigrationRemoval("spoken", selected_history.name))
            for source, target in zip(promoted[:-1], targets[:-1]):
                cross_directory_sources[("queue", target.name)] = source
            queue_order = [
                *reversed(targets),
                *(path for path in queue_order if path.name not in target_names),
            ]
            history_order = history_order[selected_index + 1 :]

    all_paths = [*queue_order, *history_order, *failed_order]
    observed_sequences = [
        sequence
        for path in all_paths
        if (sequence := strict_sequence(path.name)) is not None
    ]
    catalog = existing or catalog_seeded_above(observed_sequences)
    if existing is not None and observed_sequences:
        catalog = IdentityCatalog(
            max(catalog.next_sequence, max(observed_sequences) + 1),
            dict(catalog.ids_by_sequence),
        )
    existing_ids = set(catalog.ids_by_sequence.values())
    used_sequences: set[int] = set()
    assignments: dict[tuple[str, str], tuple[str, str]] = {}
    moves: list[MigrationMove] = []
    sections = (
        ("queue", queue_order),
        ("spoken", history_order),
        ("failed", failed_order),
    )
    for directory_name, paths in sections:
        for path in paths:
            sequence = strict_sequence(path.name)
            if sequence is not None and sequence not in used_sequences:
                public_id = catalog.public_id(sequence)
                if public_id is None:
                    public_id = _fresh_public_id(existing_ids)
                    catalog = catalog_with_mapping(catalog, sequence, public_id)
                target_name = path.name
            else:
                catalog, sequence, public_id = allocate_identity(catalog)
                while any(
                    strict_sequence(existing_path.name) == sequence
                    for existing_path in all_paths
                ):
                    catalog, sequence, public_id = allocate_identity(catalog)
                target_name = (
                    f"{sequence:03d}-{_legacy_filename_tail(path.name, default_voice)}"
                )
                if (directory_name, path.name) not in cross_directory_sources:
                    moves.append(
                        MigrationMove(
                            directory_name,
                            directory_name,
                            path.name,
                            target_name,
                        )
                    )
            used_sequences.add(sequence)
            assignments[(directory_name, path.name)] = (public_id, target_name)

    for (directory_name, target_name), source in cross_directory_sources.items():
        final_target = assignments[(directory_name, target_name)][1]
        moves.append(
            MigrationMove("spoken", directory_name, source.name, final_target)
        )

    migration = IdentityMigration(
        catalog,
        tuple(moves),
        tuple(removals),
        tuple(assignments[("queue", path.name)][0] for path in queue_order),
        tuple(assignments[("spoken", path.name)][0] for path in history_order),
        len(all_paths),
    )
    needs_migration = (
        existing is None
        or migration.moves
        or migration.removals
        or read_order_payload(queue_order_path)[0] != 2
        or read_order_payload(history_order_path)[0] != 2
        or catalog != existing
    )
    return migration if needs_migration else None


def migration_from_intent(payload: object) -> IdentityMigration:
    """Validate a saved migration before it is allowed to touch storage."""
    if not isinstance(payload, dict) or payload.get("operation") != "identity_migration":
        raise ValueError("invalid pending identity migration")
    try:
        catalog = catalog_from_payload(payload.get("catalog"))
        raw_moves = payload["moves"]
        raw_removals = payload["removals"]
        raw_queue_ids = payload["queue_ids"]
        raw_history_ids = payload["history_ids"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid pending identity migration") from error
    if not (
        isinstance(raw_moves, list)
        and isinstance(raw_removals, list)
        and isinstance(raw_queue_ids, list)
        and all(is_public_id(item) for item in raw_queue_ids)
        and isinstance(raw_history_ids, list)
        and all(is_public_id(item) for item in raw_history_ids)
    ):
        raise ValueError("invalid pending identity migration")
    sections = {"queue", "spoken", "failed"}
    moves: list[MigrationMove] = []
    for raw_move in raw_moves:
        if not isinstance(raw_move, dict):
            raise TypeError("invalid pending identity migration")
        values = (
            raw_move.get("source_directory"),
            raw_move.get("target_directory"),
            raw_move.get("source"),
            raw_move.get("target"),
        )
        if not (
            values[0] in sections
            and values[1] in sections
            and isinstance(values[2], str)
            and Path(values[2]).name == values[2]
            and isinstance(values[3], str)
            and Path(values[3]).name == values[3]
        ):
            raise ValueError("invalid pending identity migration")
        moves.append(MigrationMove(*values))
    removals: list[MigrationRemoval] = []
    for raw_removal in raw_removals:
        if not isinstance(raw_removal, dict):
            raise TypeError("invalid pending identity migration")
        directory = raw_removal.get("directory")
        name = raw_removal.get("name")
        if directory not in sections or not isinstance(name, str) or Path(name).name != name:
            raise ValueError("invalid pending identity migration")
        removals.append(MigrationRemoval(directory, name))
    return IdentityMigration(
        catalog,
        tuple(moves),
        tuple(removals),
        tuple(raw_queue_ids),
        tuple(raw_history_ids),
    )
