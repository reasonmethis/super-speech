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
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

PUBLIC_ID_PATTERN = re.compile(r"sp_[a-f0-9]{32}\Z")
VOICE_PATTERN = re.compile(r"[ab][fm]_[a-z0-9_]+\Z")
SPEECHICLE_FILENAME_PATTERN = re.compile(
    r"(?P<sequence>[0-9]{3,})-"
    r"(?P<public_id>sp_[a-f0-9]{32})-"
    r"(?P<voice>[ab][fm]_[a-z0-9_]+)"
    r"(?:-g(?P<gap_ms>[0-9]+))?-say\.txt\Z"
)
STRICT_SEQUENCE_PATTERN = re.compile(r"([0-9]+)-.+\.txt\Z")
CATALOG_VERSION = 1
EMBED_PUBLIC_IDS_VERSION = 3
MigrationRoot = Literal["queue", "spoken", "failed"]
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class SpeechicleFilename:
    """A canonical storage name with public identity and playback metadata.

    The sequence preserves local chronology. The random public ID is the durable
    identity and remains unchanged when voice metadata changes.
    """

    sequence: int
    public_id: str
    voice: str
    gap_ms: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("speech sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("speech sequence must be positive")
        if not is_public_id(self.public_id):
            raise ValueError("speech public ID is invalid")
        if VOICE_PATTERN.fullmatch(self.voice) is None:
            raise ValueError("speech voice is invalid")
        if self.gap_ms is not None and (
            isinstance(self.gap_ms, bool)
            or not isinstance(self.gap_ms, int)
            or not 0 <= self.gap_ms <= 1500
        ):
            raise ValueError("speech gap must be between 0 and 1500 milliseconds")

    @classmethod
    def parse(cls, name: str) -> SpeechicleFilename:
        """Parse one exact canonical filename without accepting normalized variants."""
        if not isinstance(name, str):
            raise TypeError("speech filename must be a string")
        match = SPEECHICLE_FILENAME_PATTERN.fullmatch(name)
        if match is None:
            raise ValueError("speech filename is not canonical")
        gap = match.group("gap_ms")
        parsed = cls(
            int(match.group("sequence")),
            match.group("public_id"),
            match.group("voice"),
            int(gap) if gap is not None else None,
        )
        if parsed.render() != name:
            raise ValueError("speech filename is not canonical")
        return parsed

    def render(self) -> str:
        gap = f"-g{self.gap_ms}" if self.gap_ms is not None else ""
        return f"{self.sequence:03d}-{self.public_id}-{self.voice}{gap}-say.txt"

    def with_voice(self, voice: str) -> SpeechicleFilename:
        return SpeechicleFilename(self.sequence, self.public_id, voice, self.gap_ms)


@dataclass(frozen=True, slots=True)
class IdentityCatalog:
    """The legacy sequence-to-public-ID map read during filename upgrades."""

    next_sequence: int
    ids_by_sequence: dict[int, str]

    def public_id(self, sequence: int) -> str | None:
        return self.ids_by_sequence.get(sequence)


@dataclass(frozen=True, slots=True)
class MigrationMove:
    source_directory: str
    target_directory: str
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class MigrationRemoval:
    directory: str
    name: str


@dataclass(frozen=True, slots=True)
class IdentityMigration:
    catalog: IdentityCatalog
    moves: tuple[MigrationMove, ...]
    removals: tuple[MigrationRemoval, ...]
    queue_ids: tuple[str, ...]
    history_ids: tuple[str, ...]

    def intent_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "operation": "identity_migration",
            "moves": [asdict(move) for move in self.moves],
            "removals": [asdict(removal) for removal in self.removals],
            "catalog": catalog_payload(self.catalog),
            "queue_ids": list(self.queue_ids),
            "history_ids": list(self.history_ids),
        }


def is_public_id(value: object) -> bool:
    return isinstance(value, str) and PUBLIC_ID_PATTERN.fullmatch(value) is not None


def generate_public_id(
    existing_ids: Collection[str] = (),
    *,
    generate: Callable[[], str] | None = None,
) -> str:
    """Generate one valid public ID that does not occur in the supplied collection."""
    generate_id = generate or (lambda: f"sp_{secrets.token_hex(16)}")
    existing = set(existing_ids)
    public_id = generate_id()
    while not is_public_id(public_id) or public_id in existing:
        public_id = generate_id()
    return public_id


@dataclass(frozen=True, slots=True)
class EmbedPublicIdsFile:
    source_root: MigrationRoot
    source_name: str
    target_name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ExactReplayDuplicateRemoval:
    source_root: MigrationRoot
    source_name: str
    duplicate_root: MigrationRoot
    duplicate_name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class EmbedPublicIdsMigration:
    """A complete final file and order inventory for embedding public IDs.

    This migration does not preserve the retired catalog's last sequence.
    Startup sets the new counter above the sequence in every migrated filename.
    Public IDs, not sequence numbers, identify Speechicles.
    """

    queue_files: tuple[EmbedPublicIdsFile, ...]
    history_files: tuple[EmbedPublicIdsFile, ...]
    failed_files: tuple[EmbedPublicIdsFile, ...]
    removals: tuple[ExactReplayDuplicateRemoval, ...]

    @property
    def files(self) -> tuple[EmbedPublicIdsFile, ...]:
        return (*self.queue_files, *self.history_files, *self.failed_files)

    @property
    def queue_ids(self) -> tuple[str, ...]:
        return tuple(
            SpeechicleFilename.parse(item.target_name).public_id
            for item in self.queue_files
        )

    @property
    def history_ids(self) -> tuple[str, ...]:
        return tuple(
            SpeechicleFilename.parse(item.target_name).public_id
            for item in self.history_files
        )

    def intent_payload(self) -> dict[str, object]:
        return {
            "version": EMBED_PUBLIC_IDS_VERSION,
            "operation": "embed_public_ids",
            "queue_files": [asdict(item) for item in self.queue_files],
            "history_files": [asdict(item) for item in self.history_files],
            "failed_files": [asdict(item) for item in self.failed_files],
            "removals": [asdict(item) for item in self.removals],
        }


def _is_windows_safe_component(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or value.endswith((".", " "))
        or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in value)
    ):
        return False
    basename = value.split(".", 1)[0].upper()
    return basename not in WINDOWS_RESERVED_BASENAMES


def _is_windows_safe_text_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and _is_windows_safe_component(value)
        and value.lower().endswith(".txt")
    )


def _validate_embed_public_ids_migration(
    migration: EmbedPublicIdsMigration,
) -> EmbedPublicIdsMigration:
    source_keys: set[tuple[str, str]] = set()
    target_keys: set[tuple[str, str]] = set()
    target_ids: set[str] = set()
    target_sequences: set[int] = set()
    file_locations: list[tuple[tuple[str, str], tuple[str, str]]] = []
    file_by_source: dict[tuple[str, str], EmbedPublicIdsFile] = {}
    allowed_sources = {
        "queue": frozenset({"queue", "spoken"}),
        "spoken": frozenset({"spoken"}),
        "failed": frozenset({"failed"}),
    }
    collections = (
        ("queue", migration.queue_files),
        ("spoken", migration.history_files),
        ("failed", migration.failed_files),
    )
    for target_root, files in collections:
        for item in files:
            if (
                item.source_root not in allowed_sources[target_root]
                or not _is_windows_safe_text_filename(item.source_name)
                or not _is_windows_safe_text_filename(item.target_name)
                or SHA256_PATTERN.fullmatch(item.sha256) is None
            ):
                raise ValueError("embed_public_ids contains an invalid file entry")
            filename = SpeechicleFilename.parse(item.target_name)
            source_key = (item.source_root, item.source_name)
            target_key = (target_root, item.target_name)
            if source_key in source_keys:
                raise ValueError("embed_public_ids contains a duplicate source")
            if target_key in target_keys:
                raise ValueError("embed_public_ids contains a target collision")
            if filename.public_id in target_ids or filename.sequence in target_sequences:
                raise ValueError("embed_public_ids target identities are not unique")
            source_keys.add(source_key)
            target_keys.add(target_key)
            target_ids.add(filename.public_id)
            target_sequences.add(filename.sequence)
            file_locations.append((source_key, target_key))
            file_by_source[source_key] = item

    if any(
        target_key in source_keys and target_key != source_key
        for source_key, target_key in file_locations
    ):
        raise ValueError("embed_public_ids target is another file's source")

    removal_keys: set[tuple[str, str]] = set()
    if len(migration.removals) > 1:
        raise ValueError("embed_public_ids contains multiple replay removals")
    for removal in migration.removals:
        if (
            removal.source_root != "spoken"
            or removal.duplicate_root != "queue"
            or not _is_windows_safe_text_filename(removal.source_name)
            or not _is_windows_safe_text_filename(removal.duplicate_name)
            or removal.source_name != removal.duplicate_name
            or SHA256_PATTERN.fullmatch(removal.sha256) is None
        ):
            raise ValueError("embed_public_ids contains an invalid replay removal")
        removal_key = (removal.source_root, removal.source_name)
        duplicate_key = (removal.duplicate_root, removal.duplicate_name)
        duplicate = file_by_source.get(duplicate_key)
        if (
            removal_key in removal_keys
            or removal_key in source_keys
            or removal_key in target_keys
            or duplicate is None
            or duplicate.sha256 != removal.sha256
        ):
            raise ValueError("embed_public_ids replay removal is ambiguous")
        removal_keys.add(removal_key)
    return migration


def embed_public_ids_from_intent(payload: object) -> EmbedPublicIdsMigration:
    """Parse a self-contained version-3 public-ID migration journal."""
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "operation",
        "queue_files",
        "history_files",
        "failed_files",
        "removals",
    }:
        raise ValueError("invalid pending embed_public_ids migration")
    if (
        not isinstance(payload["version"], int)
        or isinstance(payload["version"], bool)
        or payload["version"] != EMBED_PUBLIC_IDS_VERSION
        or payload["operation"] != "embed_public_ids"
        or not isinstance(payload["queue_files"], list)
        or not isinstance(payload["history_files"], list)
        or not isinstance(payload["failed_files"], list)
        or not isinstance(payload["removals"], list)
    ):
        raise ValueError("invalid pending embed_public_ids migration")

    def parse_files(field: str) -> tuple[EmbedPublicIdsFile, ...]:
        files: list[EmbedPublicIdsFile] = []
        for raw_file in payload[field]:
            if not isinstance(raw_file, dict) or set(raw_file) != {
                "source_root",
                "source_name",
                "target_name",
                "sha256",
            }:
                raise ValueError("invalid pending embed_public_ids migration")
            files.append(EmbedPublicIdsFile(**raw_file))
        return tuple(files)

    removals: list[ExactReplayDuplicateRemoval] = []
    for raw_removal in payload["removals"]:
        if not isinstance(raw_removal, dict) or set(raw_removal) != {
            "source_root",
            "source_name",
            "duplicate_root",
            "duplicate_name",
            "sha256",
        }:
            raise ValueError("invalid pending embed_public_ids migration")
        removals.append(ExactReplayDuplicateRemoval(**raw_removal))
    migration = EmbedPublicIdsMigration(
        parse_files("queue_files"),
        parse_files("history_files"),
        parse_files("failed_files"),
        tuple(removals),
    )
    return _validate_embed_public_ids_migration(migration)


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


def legacy_sequence(filename: str) -> int | None:
    prefix = filename.split("-", 1)[0]
    match = re.match(r"[0-9]+", prefix)
    return int(match.group()) if match else None


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


@dataclass(frozen=True, slots=True)
class _OrderSidecar:
    version: Literal[1, 2] | None
    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _InventorySpeech:
    source_root: MigrationRoot
    path: Path
    canonical: SpeechicleFilename | None
    legacy_sequence: int | None
    voice: str
    gap_ms: int | None
    sha256: str

    @property
    def source_key(self) -> tuple[str, str]:
        return self.source_root, self.path.name


def _read_strict_order_sidecar(path: Path) -> _OrderSidecar:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _OrderSidecar(None, ())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read speech order: {path.name}") from error
    if not isinstance(payload, dict) or set(payload) != {"version", "ids"}:
        raise ValueError(f"invalid speech order: {path.name}")
    version = payload["version"]
    raw_ids = payload["ids"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in {1, 2}
        or not isinstance(raw_ids, list)
        or not all(isinstance(item, str) for item in raw_ids)
        or len(set(raw_ids)) != len(raw_ids)
    ):
        raise ValueError(f"invalid speech order: {path.name}")
    if version == 1:
        if any(
            not _is_windows_safe_component(item) or item.lower().endswith(".txt")
            for item in raw_ids
        ):
            raise ValueError(f"invalid speech order: {path.name}")
    elif not all(is_public_id(item) for item in raw_ids):
        raise ValueError(f"invalid speech order: {path.name}")
    return _OrderSidecar(version, tuple(raw_ids))


def _legacy_metadata(name: str, default_voice: str) -> tuple[int | None, str, int | None]:
    sequence = legacy_sequence(name)
    if sequence is not None and sequence < 1:
        sequence = None
    tail = re.search(
        r"(?P<voice>[ab][fm]_[a-z0-9_]+)"
        r"(?:-g(?P<gap_ms>[0-9]+))?-say\.txt\Z",
        name,
        re.IGNORECASE,
    )
    if tail is None:
        return sequence, default_voice, None
    raw_gap = tail.group("gap_ms")
    gap_ms = int(raw_gap) if raw_gap is not None else None
    if gap_ms is not None and not 0 <= gap_ms <= 1500:
        return sequence, default_voice, None
    return sequence, tail.group("voice").lower(), gap_ms


def _inventory_speech(
    roots: dict[MigrationRoot, Path],
    default_voice: str,
) -> tuple[_InventorySpeech, ...]:
    if VOICE_PATTERN.fullmatch(default_voice) is None:
        raise ValueError("default voice is invalid")
    rows: list[_InventorySpeech] = []
    for root in ("queue", "spoken", "failed"):
        for path in sorted(roots[root].glob("*.txt"), key=lambda item: item.name):
            try:
                canonical = SpeechicleFilename.parse(path.name)
            except ValueError:
                canonical = None
            if canonical is None and re.search(r"sp_[a-f0-9]{32}", path.name):
                raise ValueError(f"malformed canonical speech filename: {path.name}")
            sequence, voice, gap_ms = (
                (canonical.sequence, canonical.voice, canonical.gap_ms)
                if canonical is not None
                else _legacy_metadata(path.name, default_voice)
            )
            rows.append(
                _InventorySpeech(
                    root,
                    path,
                    canonical,
                    sequence,
                    voice,
                    gap_ms,
                    sha256(path.read_bytes()).hexdigest(),
                )
            )
    return tuple(rows)


def _provisional_public_id(
    row: _InventorySpeech,
    catalog: IdentityCatalog | None,
) -> str | None:
    if row.canonical is not None:
        return row.canonical.public_id
    if row.legacy_sequence is None or catalog is None:
        return None
    return catalog.public_id(row.legacy_sequence)


def _storage_order_key(row: _InventorySpeech) -> tuple[bool, int, str]:
    return (
        row.legacy_sequence is None,
        row.legacy_sequence or 0,
        row.path.name,
    )


def _history_storage_order_key(row: _InventorySpeech) -> tuple[bool, int, str]:
    return (
        row.legacy_sequence is not None,
        row.legacy_sequence or 0,
        row.path.name,
    )


def _rows_in_saved_order(
    rows: list[_InventorySpeech],
    sidecar: _OrderSidecar,
    *,
    history: bool,
    catalog: IdentityCatalog | None,
) -> list[_InventorySpeech]:
    remaining = {row.path.name: row for row in rows}
    ordered: list[_InventorySpeech] = []
    for saved_id in sidecar.ids:
        matched: _InventorySpeech | None = None
        if sidecar.version == 1:
            matched = remaining.pop(f"{saved_id}.txt", None)
            if matched is None:
                saved_sequence = legacy_sequence(saved_id)
                variants = [
                    row
                    for row in remaining.values()
                    if row.legacy_sequence == saved_sequence
                ]
                if saved_sequence is None or len(variants) != 1:
                    raise ValueError(
                        f"speech order references unknown or ambiguous legacy row {saved_id}"
                    )
                matched = remaining.pop(variants[0].path.name)
        elif sidecar.version == 2:
            variants = [
                row
                for row in remaining.values()
                if _provisional_public_id(row, catalog) == saved_id
            ]
            if len(variants) > 1:
                raise ValueError(f"speech order ID {saved_id} has multiple source files")
            if not variants:
                raise ValueError(f"speech order references unknown public ID {saved_id}")
            matched = remaining.pop(variants[0].path.name)
        if matched is not None:
            ordered.append(matched)
    missing = sorted(
        remaining.values(),
        key=_history_storage_order_key if history else _storage_order_key,
        reverse=history,
    )
    return [*missing, *ordered] if history else [*ordered, *missing]


def _durable_mutation_exists(base: Path) -> bool:
    return (
        (base / "MUTATION.json").exists()
        or (base / "MUTATION.claim").exists()
        or any(base.glob("MUTATION.*.json"))
        or any(base.glob("MUTATION.*.claim"))
    )


def _load_catalog_for_legacy_embedding(
    catalog_path: Path,
    queue_order: _OrderSidecar,
    history_order: _OrderSidecar,
    base: Path,
) -> IdentityCatalog | None:
    catalog = load_catalog(catalog_path)
    if catalog is not None:
        return catalog
    if queue_order.version == 2 or history_order.version == 2:
        raise ValueError("version-2 speech order requires the identity catalog")
    if _durable_mutation_exists(base):
        raise ValueError("cannot generate IDs while a durable mutation exists")
    return None


def _row_for_saved_id(
    saved_id: str,
    sidecar: _OrderSidecar,
    rows: tuple[_InventorySpeech, ...],
    catalog: IdentityCatalog | None,
) -> _InventorySpeech | None:
    if sidecar.version == 1:
        matches = [row for row in rows if row.path.stem == saved_id]
        if not matches:
            saved_sequence = legacy_sequence(saved_id)
            matches = [
                row
                for row in rows
                if saved_sequence is not None
                and row.legacy_sequence == saved_sequence
            ]
    else:
        matches = [
            row
            for row in rows
            if _provisional_public_id(row, catalog) == saved_id
        ]
    if len(matches) > 1:
        return None
    return matches[0] if matches else None


def _repair_partial_history_boundary(
    rows: tuple[_InventorySpeech, ...],
    queue_order: _OrderSidecar,
    history_order: _OrderSidecar,
    catalog: IdentityCatalog | None,
) -> tuple[list[_InventorySpeech], list[_InventorySpeech]] | None:
    if queue_order.version is None or history_order.version is None:
        return None
    saved_queue = [
        _row_for_saved_id(saved_id, queue_order, rows, catalog)
        for saved_id in queue_order.ids
    ]
    saved_history = [
        _row_for_saved_id(saved_id, history_order, rows, catalog)
        for saved_id in history_order.ids
    ]
    if any(row is None for row in (*saved_queue, *saved_history)):
        return None
    queue_rows = [row for row in saved_queue if row is not None]
    history_rows = [row for row in saved_history if row is not None]
    if len(set((*queue_rows, *history_rows))) != len(queue_rows) + len(history_rows):
        return None
    boundary = 0
    while boundary < len(history_rows) and history_rows[boundary].source_root == "queue":
        boundary += 1
    if boundary == 0 or any(
        row.source_root != "spoken" for row in history_rows[boundary:]
    ):
        return None
    physical_queue = {row for row in rows if row.source_root == "queue"}
    physical_history = {row for row in rows if row.source_root == "spoken"}
    if physical_queue != set((*queue_rows, *history_rows[:boundary])) or physical_history != set(
        history_rows[boundary:]
    ):
        return None
    return [*reversed(history_rows[:boundary]), *queue_rows], history_rows[boundary:]


def _repair_exact_replay_duplicate(
    queue_order: list[_InventorySpeech],
    history_order: list[_InventorySpeech],
) -> tuple[
    list[_InventorySpeech],
    list[_InventorySpeech],
    tuple[ExactReplayDuplicateRemoval, ...],
]:
    queue_by_name = {row.path.name: row for row in queue_order}
    exact_duplicates = [
        (index, archived, active)
        for index, archived in enumerate(history_order)
        if (active := queue_by_name.get(archived.path.name)) is not None
        and active.sha256 == archived.sha256
    ]
    if len(exact_duplicates) != 1:
        return queue_order, history_order, ()
    selected_index, archived, active = exact_duplicates[0]
    promoted = history_order[:selected_index]
    conflicting_names = {
        row.path.name for row in promoted
    } & {row.path.name for row in queue_order}
    if conflicting_names:
        raise ValueError("exact replay repair has a target collision")
    final_queue = [
        active,
        *reversed(promoted),
        *(row for row in queue_order if row is not active),
    ]
    removal = ExactReplayDuplicateRemoval(
        archived.source_root,
        archived.path.name,
        active.source_root,
        active.path.name,
        active.sha256,
    )
    return final_queue, history_order[selected_index + 1 :], (removal,)


def _catalog_owner_for_duplicate_sequence(
    rows: list[_InventorySpeech],
    public_id: str,
    queue_order: _OrderSidecar,
    history_order: _OrderSidecar,
) -> _InventorySpeech:
    evidence: list[_InventorySpeech] = []
    for root, sidecar in (("queue", queue_order), ("spoken", history_order)):
        if sidecar.version != 2 or public_id not in sidecar.ids:
            continue
        candidates = [row for row in rows if row.source_root == root]
        if len(candidates) != 1:
            raise ValueError("version-2 order cannot prove duplicate sequence ownership")
        evidence.extend(candidates)
    if len(evidence) != 1:
        raise ValueError("version-2 order cannot prove duplicate sequence ownership")
    return evidence[0]


def _assign_legacy_filenames(
    rows: list[_InventorySpeech],
    catalog: IdentityCatalog | None,
    queue_sidecar: _OrderSidecar,
    history_sidecar: _OrderSidecar,
    generate: Callable[[], str] | None,
) -> dict[_InventorySpeech, SpeechicleFilename]:
    assignments: dict[_InventorySpeech, SpeechicleFilename] = {}
    used_ids = set(catalog.ids_by_sequence.values()) if catalog is not None else set()
    used_sequences: set[int] = set()
    groups: dict[int, list[_InventorySpeech]] = {}
    for row in rows:
        if row.legacy_sequence is not None:
            groups.setdefault(row.legacy_sequence, []).append(row)

    for sequence, group in groups.items():
        catalog_id = catalog.public_id(sequence) if catalog is not None else None
        if catalog is not None and catalog_id is None:
            raise ValueError(f"identity catalog has no ID for sequence {sequence}")
        owner = (
            _catalog_owner_for_duplicate_sequence(
                group, catalog_id, queue_sidecar, history_sidecar
            )
            if catalog_id is not None and len(group) > 1
            else group[0]
        )
        public_id = catalog_id or generate_public_id(used_ids, generate=generate)
        filename = SpeechicleFilename(sequence, public_id, owner.voice, owner.gap_ms)
        assignments[owner] = filename
        used_ids.add(filename.public_id)
        used_sequences.add(sequence)

    observed_sequences = [
        row.legacy_sequence for row in rows if row.legacy_sequence is not None
    ]
    next_sequence = max(
        [
            *observed_sequences,
            *(catalog.ids_by_sequence if catalog is not None else ()),
            (catalog.next_sequence - 1) if catalog is not None else 0,
        ],
        default=0,
    ) + 1
    for row in rows:
        if row in assignments:
            continue
        while next_sequence in used_sequences:
            next_sequence += 1
        public_id = generate_public_id(used_ids, generate=generate)
        assignments[row] = SpeechicleFilename(
            next_sequence, public_id, row.voice, row.gap_ms
        )
        used_ids.add(public_id)
        used_sequences.add(next_sequence)
        next_sequence += 1
    return assignments


def _validate_source_inventory(
    roots: dict[MigrationRoot, Path],
    rows: tuple[_InventorySpeech, ...],
) -> None:
    expected = {row.source_key: row.sha256 for row in rows}
    actual: dict[tuple[str, str], str] = {}
    for root in ("queue", "spoken", "failed"):
        for path in roots[root].glob("*.txt"):
            actual[(root, path.name)] = sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("speech files changed while planning embed_public_ids")


def plan_embed_public_ids(
    queue_directory: Path,
    spoken_directory: Path,
    failed_directory: Path,
    queue_order_path: Path,
    history_order_path: Path,
    catalog_path: Path,
    default_voice: str,
    *,
    generate: Callable[[], str] | None = None,
) -> EmbedPublicIdsMigration | None:
    """Plan one read-only migration to canonical filenames with embedded IDs."""
    roots: dict[MigrationRoot, Path] = {
        "queue": queue_directory,
        "spoken": spoken_directory,
        "failed": failed_directory,
    }
    if len(set(roots.values())) != len(roots) or len(
        {path.parent for path in roots.values()}
    ) != 1:
        raise ValueError("speech roots must be distinct siblings")
    if (queue_directory.parent / "timeline-intent.json").exists():
        raise ValueError("cannot plan embed_public_ids while timeline-intent.json exists")
    queue_sidecar = _read_strict_order_sidecar(queue_order_path)
    history_sidecar = _read_strict_order_sidecar(history_order_path)
    inventory = _inventory_speech(roots, default_voice)
    canonical_rows = [row for row in inventory if row.canonical is not None]
    if canonical_rows and len(canonical_rows) != len(inventory):
        raise ValueError("cannot replan a mixed legacy and canonical layout")
    canonical_layout = len(canonical_rows) == len(inventory)
    catalog = (
        None
        if canonical_layout
        else _load_catalog_for_legacy_embedding(
            catalog_path,
            queue_sidecar,
            history_sidecar,
            queue_directory.parent,
        )
    )

    repaired_boundary = _repair_partial_history_boundary(
        inventory, queue_sidecar, history_sidecar, catalog
    )
    removals: tuple[ExactReplayDuplicateRemoval, ...] = ()
    if repaired_boundary is not None:
        queue_rows, history_rows = repaired_boundary
    else:
        queue_rows = _rows_in_saved_order(
            [row for row in inventory if row.source_root == "queue"],
            queue_sidecar,
            history=False,
            catalog=catalog,
        )
        history_rows = _rows_in_saved_order(
            [row for row in inventory if row.source_root == "spoken"],
            history_sidecar,
            history=True,
            catalog=catalog,
        )
        queue_rows, history_rows, removals = _repair_exact_replay_duplicate(
            queue_rows, history_rows
        )
    failed_rows = sorted(
        (row for row in inventory if row.source_root == "failed"),
        key=_storage_order_key,
    )
    surviving_rows = [*queue_rows, *history_rows, *failed_rows]
    if len(set(surviving_rows)) + len(removals) != len(inventory):
        raise ValueError("embed_public_ids did not account for every speech file")
    assignments = (
        {row: row.canonical for row in surviving_rows}
        if canonical_layout
        else _assign_legacy_filenames(
            surviving_rows, catalog, queue_sidecar, history_sidecar, generate
        )
    )

    def planned_file(row: _InventorySpeech) -> EmbedPublicIdsFile:
        return EmbedPublicIdsFile(
            row.source_root,
            row.path.name,
            assignments[row].render(),
            row.sha256,
        )

    queue_files = tuple(planned_file(row) for row in queue_rows)
    history_files = tuple(planned_file(row) for row in history_rows)
    failed_files = tuple(planned_file(row) for row in failed_rows)
    migration = _validate_embed_public_ids_migration(
        EmbedPublicIdsMigration(
            queue_files,
            history_files,
            failed_files,
            removals,
        )
    )
    _validate_source_inventory(roots, inventory)
    target_collections = (
        ("queue", migration.queue_files),
        ("spoken", migration.history_files),
        ("failed", migration.failed_files),
    )
    needs_migration = (
        migration.removals
        or any(
            item.source_root != target_root or item.source_name != item.target_name
            for target_root, files in target_collections
            for item in files
        )
        or queue_sidecar.version != 2
        or history_sidecar.version != 2
        or queue_sidecar.ids != migration.queue_ids
        or history_sidecar.ids != migration.history_ids
    )
    return migration if needs_migration else None
