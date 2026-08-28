"""Durable storage for the Super Speech timeline.

Queue, History, and Failed are physical directories. Two small sidecar files
remember the display order inside Queue and History. This module owns both
representations and the journal that makes moves between them recoverable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeGuard

from file_lock import InterprocessFileLock

from speechicle_identity import (
    EmbedPublicIdsFile,
    EmbedPublicIdsMigration,
    IdentityCatalog,
    SpeechicleFilename,
    embed_public_ids_from_intent,
    is_public_id,
    load_catalog,
    migration_from_intent,
    plan_embed_public_ids,
    strict_sequence,
    write_catalog,
)


class MutationOutcomeUnconfirmed(RuntimeError):
    """The engine cannot prove what a command changed."""


def replace_path_with_confirmation(
    source: Path,
    target: Path,
    label: str,
    *,
    expected_bytes: bytes | None = None,
    missing_source_is_rejection: bool = False,
) -> None:
    """Replace one file, confirming the visible result after an OS error."""
    try:
        os.replace(source, target)
        return
    except OSError as error:
        replace_error = error

    def exists(path: Path) -> bool:
        try:
            path.stat()
            return True
        except FileNotFoundError:
            return False
        except OSError as error:
            raise MutationOutcomeUnconfirmed(
                f"could not confirm whether {label} completed"
            ) from error

    source_exists = exists(source)
    target_exists = exists(target)
    if source_exists and not target_exists:
        raise replace_error
    if (
        missing_source_is_rejection
        and not source_exists
        and not target_exists
        and isinstance(replace_error, FileNotFoundError)
    ):
        raise replace_error
    if not source_exists and target_exists:
        if expected_bytes is not None:
            try:
                matches = target.read_bytes() == expected_bytes
            except OSError as error:
                raise MutationOutcomeUnconfirmed(
                    f"could not confirm whether {label} completed"
                ) from error
            if not matches:
                raise MutationOutcomeUnconfirmed(
                    f"could not confirm whether {label} completed"
                ) from replace_error
        return
    raise MutationOutcomeUnconfirmed(
        f"could not confirm whether {label} completed"
    ) from replace_error


class HeldInstanceLock(Protocol):
    """The part of the engine process lock needed by storage preparation."""

    @property
    def held(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class TimelinePaths:
    """Every persistent path that belongs to one speech timeline."""

    root: Path

    @property
    def queue(self) -> Path:
        return self.root / "queue"

    @property
    def history(self) -> Path:
        return self.root / "spoken"

    @property
    def failed(self) -> Path:
        return self.root / "failed"

    @property
    def queue_order(self) -> Path:
        return self.root / "queue-order.json"

    @property
    def history_order(self) -> Path:
        return self.root / "history-order.json"

    @property
    def mutation_lock(self) -> Path:
        return self.root / "timeline.lock"

    @property
    def intent(self) -> Path:
        return self.root / "timeline-intent.json"

    @property
    def legacy_identity_index(self) -> Path:
        return self.root / "speechicle-index.json"

    @property
    def sequence_counter(self) -> Path:
        return self.root / "next-sequence.json"


@dataclass(frozen=True, slots=True)
class TimelineSelection:
    """Describe where a selected Speechicle is and how playback should react."""

    target: Path
    origin: Literal["current", "waiting", "history"]
    restart_playback: bool
    moved_count: int


@dataclass(frozen=True, slots=True)
class TimelineLocation:
    """Name one file inside Queue or History."""

    root: Literal["queue", "history"]
    name: str


@dataclass(frozen=True, slots=True)
class TimelineMove:
    """Move one timeline file, optionally preserving a duplicate copy."""

    source: TimelineLocation
    target: TimelineLocation
    backup: TimelineLocation | None = None
    # Keep the Queue copy when promotion finds the same Speechicle already there
    preserve_existing_target: bool = False


@dataclass(frozen=True, slots=True)
class TimelinePlan:
    """One canonical, recoverable change to the saved timeline."""

    kind: Literal["archive", "promote"]
    moves: tuple[TimelineMove, ...]
    previous_queue_ids: tuple[str, ...]
    previous_history_ids: tuple[str, ...]
    queue_ids: tuple[str, ...]
    history_ids: tuple[str, ...]

    def intent_payload(self) -> dict[str, object]:
        return {
            "version": 3,
            "operation": "timeline_plan",
            "moves": [
                {
                    "source": {"root": move.source.root, "name": move.source.name},
                    "target": {"root": move.target.root, "name": move.target.name},
                    "backup": (
                        {"root": move.backup.root, "name": move.backup.name}
                        if move.backup is not None
                        else None
                    ),
                    "preserve_existing_target": move.preserve_existing_target,
                }
                for move in self.moves
            ],
            "previous_queue_ids": list(self.previous_queue_ids),
            "previous_history_ids": list(self.previous_history_ids),
            "queue_ids": list(self.queue_ids),
            "history_ids": list(self.history_ids),
        }


@dataclass(frozen=True, slots=True)
class _LegacyTimelinePlan:
    """An upgrade-only plan whose filenames or order keys predate public IDs."""

    kind: Literal["archive", "promote"]
    moves: tuple[TimelineMove, ...]
    previous_queue_ids: tuple[str, ...]
    previous_history_ids: tuple[str, ...]
    queue_ids: tuple[str, ...]
    history_ids: tuple[str, ...]
    order_version: Literal[1, 2]


def _safe_timeline_filename(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not any(character in value for character in ("/", "\\", ":", "\0"))
        and Path(value).name == value
    )


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class TimelineStorage:
    """Own the durable speech files, their order, and crash recovery."""

    def __init__(self, paths: TimelinePaths, default_voice: str) -> None:
        self.paths = paths
        self.default_voice = default_voice
        self._lock = threading.RLock()
        self._order_cache: dict[Path, list[str]] = {}
        self._history_dirty = True
        self._history_count = 0
        self._history_items: list[dict[str, object]] = []

    @contextmanager
    def mutation(self, timeout: float | None = 10.0):
        """Serialize a complete storage decision across threads and processes."""
        # The engine waits; a separate command times out so it can return an error
        with self._lock:
            lock = InterprocessFileLock(self.paths.mutation_lock)
            deadline = None if timeout is None else time.monotonic() + timeout
            try:
                while not lock.acquire():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError(
                            "timed out waiting for the speech timeline lock"
                        )
                    time.sleep(0.01)
                yield
            finally:
                lock.release()

    def location_path(self, location: TimelineLocation) -> Path:
        directory = (
            self.paths.queue if location.root == "queue" else self.paths.history
        )
        return directory / location.name

    @staticmethod
    def canonical_filename(path: Path) -> SpeechicleFilename:
        try:
            return SpeechicleFilename.parse(path.name)
        except ValueError as error:
            raise RuntimeError(
                f"speech filename is not canonical: {path.name}"
            ) from error

    @classmethod
    def public_id(cls, path: Path) -> str:
        return cls.canonical_filename(path).public_id

    @classmethod
    def sequence(cls, path: Path) -> int | None:
        try:
            return SpeechicleFilename.parse(path.name).sequence
        except ValueError:
            return None

    @classmethod
    def queue_sort_key(cls, path: Path) -> tuple[bool, int, str]:
        sequence = cls.sequence(path)
        return (sequence is None, sequence or 0, path.name)

    @classmethod
    def history_sort_key(cls, path: Path) -> tuple[bool, int, str]:
        sequence = cls.sequence(path)
        return (sequence is not None, sequence or 0, path.name)

    def canonical_inventory(self) -> dict[Path, SpeechicleFilename]:
        inventory: dict[Path, SpeechicleFilename] = {}
        public_ids: set[str] = set()
        sequences: set[int] = set()
        for directory in (
            self.paths.queue,
            self.paths.history,
            self.paths.failed,
        ):
            for path in directory.glob("*.txt"):
                filename = self.canonical_filename(path)
                if filename.public_id in public_ids:
                    raise RuntimeError(
                        f"duplicate speech public ID: {filename.public_id}"
                    )
                if filename.sequence in sequences:
                    raise RuntimeError(
                        f"duplicate live speech sequence: {filename.sequence}"
                    )
                inventory[path] = filename
                public_ids.add(filename.public_id)
                sequences.add(filename.sequence)
        return inventory

    def find(self, directory: Path, public_id: str) -> Path | None:
        if not is_public_id(public_id):
            return None
        return next(
            (
                path
                for path in directory.glob("*.txt")
                if self.canonical_filename(path).public_id == public_id
            ),
            None,
        )

    def _write_order(self, path: Path, ids: list[str], version: int = 2) -> None:
        if not ids:
            path.unlink(missing_ok=True)
            self._order_cache[path] = []
            return
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
        )
        payload = {"version": version, "ids": ids}
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            try:
                os.replace(temporary, path)
            except OSError as replace_error:
                # Windows may report a replace error after the new bytes became visible
                try:
                    stored = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    raise replace_error
                except OSError as read_error:
                    raise MutationOutcomeUnconfirmed(
                        f"could not confirm whether {path.name} was replaced"
                    ) from read_error
                except json.JSONDecodeError:
                    raise replace_error
                if stored != payload:
                    raise replace_error
            self._order_cache[path] = list(ids)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _strict_order_ids(payload: object, name: str) -> list[str]:
        if not isinstance(payload, dict) or set(payload) != {"version", "ids"}:
            raise RuntimeError(f"invalid speech order: {name}")
        ids = payload["ids"]
        if (
            payload["version"] != 2
            or not isinstance(ids, list)
            or not all(is_public_id(item) for item in ids)
            or len(ids) != len(set(ids))
        ):
            raise RuntimeError(f"invalid speech order: {name}")
        return ids

    def _read_order(self, path: Path) -> list[str]:
        for attempt in range(3):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                result = self._strict_order_ids(payload, path.name)
                self._order_cache[path] = result
                return result
            except FileNotFoundError:
                self._order_cache[path] = []
                return []
            except OSError:
                if path in self._order_cache:
                    # A prior good order is safer than treating a short read error as empty
                    return list(self._order_cache[path])
                if attempt < 2:
                    time.sleep(0.01)
                    continue
                raise RuntimeError(f"could not read speech order: {path.name}")
            except (ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"invalid speech order: {path.name}") from error
        raise AssertionError("unreachable")

    def queue_files(self) -> list[Path]:
        """Return Queue in saved playback order, then append new files."""
        with self._lock:
            live = {
                self.public_id(path): path for path in self.paths.queue.glob("*.txt")
            }
            ordered = []
            for speechicle_id in self._read_order(self.paths.queue_order):
                matched = live.pop(speechicle_id, None)
                if matched is not None:
                    ordered.append(matched)
            ordered.extend(sorted(live.values(), key=self.queue_sort_key))
            return ordered

    def history_files(self) -> list[Path]:
        """Return History in its saved display order."""
        with self._lock:
            live = {
                self.public_id(path): path for path in self.paths.history.glob("*.txt")
            }
            ordered = [
                live.pop(speechicle_id)
                for speechicle_id in self._read_order(self.paths.history_order)
                if speechicle_id in live
            ]
            missing = sorted(
                live.values(), key=self.history_sort_key, reverse=True
            )
            # Newly archived rows belong above older History rows
            return [*missing, *ordered]

    def _saved_ids(self, directory: Path, paths: list[Path]) -> list[str]:
        live = {self.public_id(path) for path in directory.glob("*.txt")}
        ids = [self.public_id(path) for path in paths if self.public_id(path) in live]
        return list(dict.fromkeys(ids))

    def save_queue_order(self, paths: list[Path] | None = None) -> None:
        with self._lock:
            ordered = paths if paths is not None else self.queue_files()
            ids = self._saved_ids(self.paths.queue, ordered)
            missing = sorted(
                (
                    path
                    for path in self.paths.queue.glob("*.txt")
                    if self.public_id(path) not in ids
                ),
                key=self.queue_sort_key,
            )
            self._write_order(
                self.paths.queue_order,
                [*ids, *(self.public_id(path) for path in missing)],
            )

    def save_history_order(self, paths: list[Path] | None = None) -> None:
        with self._lock:
            ordered = paths if paths is not None else self.history_files()
            ids = self._saved_ids(self.paths.history, ordered)
            missing = sorted(
                (
                    path
                    for path in self.paths.history.glob("*.txt")
                    if self.public_id(path) not in ids
                ),
                key=self.history_sort_key,
                reverse=True,
            )
            self._write_order(
                self.paths.history_order,
                [*ids, *(self.public_id(path) for path in missing)],
            )

    def _write_intent(self, payload: dict[str, object]) -> None:
        if self.paths.intent.exists():
            raise RuntimeError("a timeline transaction is already pending recovery")
        temporary = self.paths.intent.with_name(
            f"{self.paths.intent.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            serialized = json.dumps(payload).encode("utf-8")
            temporary.write_bytes(serialized)
            replace_path_with_confirmation(
                temporary,
                self.paths.intent,
                "timeline intent publication",
                expected_bytes=serialized,
            )
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _derived_public_id(namespace: str, sequence: int) -> str:
        if sequence > 0xFFFFFFFFFFFFFFFF:
            raise RuntimeError("speech sequence counter is exhausted")
        return f"sp_{namespace}{sequence:016x}"

    def _read_sequence_counter(self) -> tuple[str, int]:
        try:
            payload = json.loads(
                self.paths.sequence_counter.read_text(encoding="utf-8")
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "speech sequence counter is missing; restart the engine"
            ) from error
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("speech sequence counter is invalid") from error
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "namespace",
            "next_sequence",
        }:
            raise RuntimeError("speech sequence counter is invalid")
        namespace = payload["namespace"]
        next_sequence = payload["next_sequence"]
        if (
            payload["version"] != 1
            or not isinstance(namespace, str)
            or re.fullmatch(r"[a-f0-9]{16}", namespace) is None
            or isinstance(next_sequence, bool)
            or not isinstance(next_sequence, int)
            or not 1 <= next_sequence <= 0xFFFFFFFFFFFFFFFF
        ):
            raise RuntimeError("speech sequence counter is invalid")
        return namespace, next_sequence

    def _write_sequence_counter(self, namespace: str, next_sequence: int) -> None:
        payload = {
            "version": 1,
            "namespace": namespace,
            "next_sequence": next_sequence,
        }
        temporary = self.paths.sequence_counter.with_name(
            f"{self.paths.sequence_counter.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self.paths.sequence_counter)
        finally:
            temporary.unlink(missing_ok=True)

    def _prepare_sequence_counter(
        self, inventory: dict[Path, SpeechicleFilename]
    ) -> None:
        max_sequence = max(
            (filename.sequence for filename in inventory.values()), default=0
        )
        if max_sequence == 0xFFFFFFFFFFFFFFFF:
            raise RuntimeError("speech sequence counter is exhausted")
        try:
            namespace, next_sequence = self._read_sequence_counter()
        except RuntimeError:
            if self.paths.sequence_counter.exists():
                raise
            used_prefixes = {
                filename.public_id.removeprefix("sp_")[:16]
                for filename in inventory.values()
            }
            # Never adopt a namespace already present in an older public ID
            namespace = os.urandom(8).hex()
            while namespace in used_prefixes:
                namespace = os.urandom(8).hex()
            self._write_sequence_counter(namespace, max_sequence + 1)
            return

        for filename in inventory.values():
            public_hex = filename.public_id.removeprefix("sp_")
            if public_hex.startswith(namespace) and filename.public_id != self._derived_public_id(
                namespace, filename.sequence
            ):
                raise RuntimeError("speech sequence namespace collides with stored identity")
        normalized_next = max(next_sequence, max_sequence + 1)
        if normalized_next != next_sequence:
            self._write_sequence_counter(namespace, normalized_next)

    def reserve(self, voice: str, gap_ms: int | None, text: str) -> Path:
        """Create one Queue file without enumerating the stored timeline."""
        with self.mutation():
            self._recover_current_plan()
            namespace, sequence = self._read_sequence_counter()
            if sequence == 0xFFFFFFFFFFFFFFFF:
                raise RuntimeError("speech sequence counter is exhausted")
            # Save the number first so a failed Queue write cannot reuse an ID
            self._write_sequence_counter(namespace, sequence + 1)
            public_id = self._derived_public_id(namespace, sequence)
            path = self.paths.queue / SpeechicleFilename(
                sequence, public_id, voice, gap_ms
            ).render()
            temporary = self.paths.queue / (
                f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            try:
                serialized = text.encode("utf-8")
                temporary.write_bytes(serialized)
                replace_path_with_confirmation(
                    temporary,
                    path,
                    "Queue file publication",
                    expected_bytes=serialized,
                )
                return path
            finally:
                temporary.unlink(missing_ok=True)

    def invalidate_history(self) -> None:
        with self._lock:
            self._history_dirty = True

    def history_snapshot(self, limit: int) -> tuple[int, list[dict[str, object]]]:
        """Return the cached bounded History view, refreshing after mutations."""
        with self._lock:
            if self._history_dirty:
                history_files = self.history_files()
                items: list[dict[str, object]] = []
                read_failed = False
                previous = {str(item["id"]): item for item in self._history_items}
                for path in history_files[:limit]:
                    speechicle_id = self.public_id(path)
                    try:
                        text = path.read_text(encoding="utf-8").strip()
                    except OSError:
                        read_failed = True
                        text = str(previous.get(speechicle_id, {}).get("text", ""))
                    items.append(
                        {
                            "id": speechicle_id,
                            "text": text,
                            "voice": self.canonical_filename(path).voice,
                        }
                    )
                self._history_count = len(history_files)
                self._history_items = items
                self._history_dirty = read_failed
            return self._history_count, self._history_items

    def _parse_location(self, value: object, label: str) -> TimelineLocation:
        if not isinstance(value, dict) or set(value) != {"root", "name"}:
            raise RuntimeError(f"invalid {label} location")
        root = value["root"]
        name = value["name"]
        if root not in {"queue", "history"}:
            raise RuntimeError(f"invalid {label} storage root")
        if not _safe_timeline_filename(name):
            raise RuntimeError(f"invalid {label} filename")
        return TimelineLocation(root, name)

    @staticmethod
    def _parse_ids(value: object, label: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(is_public_id(item) for item in value):
            raise RuntimeError(f"invalid {label}")
        if len(value) != len(set(value)):
            raise RuntimeError(f"duplicate {label}")
        return tuple(value)

    def _validate_plan(self, plan: TimelinePlan) -> None:
        """Reject plans whose files and orders describe different changes."""
        if not plan.moves:
            raise RuntimeError("timeline plan has no moves")
        for label, ids in (
            ("previous Queue IDs", plan.previous_queue_ids),
            ("previous History IDs", plan.previous_history_ids),
            ("final Queue IDs", plan.queue_ids),
            ("final History IDs", plan.history_ids),
        ):
            if not all(is_public_id(item) for item in ids) or len(ids) != len(set(ids)):
                raise RuntimeError(f"invalid {label}")

        sources: set[TimelineLocation] = set()
        targets: set[TimelineLocation] = set()
        backups: set[TimelineLocation] = set()
        source_sequences: set[int] = set()
        target_sequences: set[int] = set()
        expected_roots = (
            ("queue", "history")
            if plan.kind == "archive"
            else ("history", "queue")
        )
        move_ids: set[str] = set()
        for move in plan.moves:
            try:
                source_filename = SpeechicleFilename.parse(move.source.name)
                target_filename = SpeechicleFilename.parse(move.target.name)
            except ValueError as error:
                raise RuntimeError("invalid canonical timeline filename") from error
            if (
                source_filename.sequence != target_filename.sequence
                or source_filename.public_id != target_filename.public_id
                or source_filename.gap_ms != target_filename.gap_ms
            ):
                raise RuntimeError("timeline move changes canonical identity metadata")
            if (move.source.root, move.target.root) != expected_roots:
                raise RuntimeError("timeline move contradicts its plan kind")
            if (
                move.source in sources
                or move.target in targets
                or move.source in backups
                or move.target in backups
                or source_filename.sequence in source_sequences
                or target_filename.sequence in target_sequences
            ):
                raise RuntimeError("timeline plan contains a duplicate move")
            sources.add(move.source)
            targets.add(move.target)
            source_sequences.add(source_filename.sequence)
            target_sequences.add(target_filename.sequence)
            move_ids.add(source_filename.public_id)
            backup = move.backup
            if backup is None:
                if move.preserve_existing_target:
                    raise RuntimeError("preserved timeline target has no backup")
                continue
            if (
                backup.root != "history"
                or not backup.name.startswith(f".{move.source.name}.")
                or not backup.name.endswith(".duplicate")
                or backup in backups
                or backup in sources
                or backup in targets
                or backup in {move.source, move.target}
            ):
                raise RuntimeError("invalid timeline backup")
            backups.add(backup)
            if move.preserve_existing_target != (plan.kind == "promote"):
                raise RuntimeError("timeline backup contradicts its move direction")

        previous_queue = set(plan.previous_queue_ids)
        previous_history = set(plan.previous_history_ids)
        final_queue = set(plan.queue_ids)
        final_history = set(plan.history_ids)
        if previous_queue & previous_history or final_queue & final_history:
            raise RuntimeError("timeline orders contain the same ID in both sections")
        if plan.kind == "archive":
            moved_ids = previous_queue - final_queue
            valid_change = (
                moved_ids == final_history - previous_history
                and final_queue <= previous_queue
                and previous_history <= final_history
            )
        else:
            moved_ids = previous_history - final_history
            valid_change = (
                moved_ids == final_queue - previous_queue
                and final_history <= previous_history
                and previous_queue <= final_queue
            )
        if not valid_change:
            raise RuntimeError(f"timeline {plan.kind} orders contradict its moves")
        if len(moved_ids) != len(plan.moves) or moved_ids != move_ids:
            raise RuntimeError("timeline move files contradict its orders")

    def _parse_plan(self, payload: object) -> TimelinePlan:
        expected_fields = {
            "version",
            "operation",
            "moves",
            "previous_queue_ids",
            "previous_history_ids",
            "queue_ids",
            "history_ids",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise RuntimeError("invalid pending timeline plan")
        if payload["version"] != 3 or payload["operation"] != "timeline_plan":
            raise RuntimeError("unsupported pending timeline plan")
        raw_moves = payload["moves"]
        if not isinstance(raw_moves, list):
            raise RuntimeError("invalid pending timeline moves")
        moves: list[TimelineMove] = []
        kinds: set[Literal["archive", "promote"]] = set()
        for raw_move in raw_moves:
            expected_move_fields = {
                "source",
                "target",
                "backup",
                "preserve_existing_target",
            }
            if not isinstance(raw_move, dict) or set(raw_move) != expected_move_fields:
                raise RuntimeError("invalid pending timeline move")
            source = self._parse_location(raw_move["source"], "timeline source")
            target = self._parse_location(raw_move["target"], "timeline target")
            roots = (source.root, target.root)
            if roots == ("queue", "history"):
                kinds.add("archive")
            elif roots == ("history", "queue"):
                kinds.add("promote")
            else:
                raise RuntimeError("invalid timeline move direction")
            raw_backup = raw_move["backup"]
            backup = (
                None
                if raw_backup is None
                else self._parse_location(raw_backup, "timeline backup")
            )
            preserve = raw_move["preserve_existing_target"]
            if not isinstance(preserve, bool):
                raise RuntimeError("invalid timeline duplicate policy")
            moves.append(TimelineMove(source, target, backup, preserve))
        if len(kinds) != 1:
            raise RuntimeError("timeline plan mixes move directions")
        plan = TimelinePlan(
            kinds.pop(),
            tuple(moves),
            self._parse_ids(payload["previous_queue_ids"], "previous Queue IDs"),
            self._parse_ids(
                payload["previous_history_ids"], "previous History IDs"
            ),
            self._parse_ids(payload["queue_ids"], "final Queue IDs"),
            self._parse_ids(payload["history_ids"], "final History IDs"),
        )
        self._validate_plan(plan)
        return plan

    def _write_plan_orders(
        self,
        plan: TimelinePlan,
        queue_ids: tuple[str, ...],
        history_ids: tuple[str, ...],
        written_orders: list[Path],
    ) -> None:
        writes = (
            (
                (self.paths.history_order, history_ids),
                (self.paths.queue_order, queue_ids),
            )
            if plan.kind == "archive"
            else (
                (self.paths.queue_order, queue_ids),
                (self.paths.history_order, history_ids),
            )
        )
        for path, ids in writes:
            self._write_order(path, list(ids))
            written_orders.append(path)

    def _apply_move(self, move: TimelineMove) -> bool:
        source = self.location_path(move.source)
        target = self.location_path(move.target)
        backup = self.location_path(move.backup) if move.backup is not None else None
        if not source.exists():
            if target.exists():
                return False
            raise FileNotFoundError(source)
        if move.preserve_existing_target:
            if backup is None:
                raise RuntimeError("timeline duplicate has no backup path")
            os.replace(source, backup)
        else:
            if backup is not None:
                os.replace(target, backup)
            os.replace(source, target)
        return True

    def _rollback_move(self, move: TimelineMove) -> None:
        source = self.location_path(move.source)
        target = self.location_path(move.target)
        backup = self.location_path(move.backup) if move.backup is not None else None
        if move.preserve_existing_target:
            if backup is not None and backup.exists():
                if not source.exists():
                    os.replace(backup, source)
            elif not source.exists() and target.exists():
                os.replace(target, source)
            return
        if not source.exists() and target.exists():
            os.replace(target, source)
        if backup is not None and backup.exists():
            os.replace(backup, target)

    def _execute_plan(self, plan: TimelinePlan) -> None:
        """Apply one saved plan and try to undo completed steps after an error."""
        self._validate_plan(plan)
        applied_moves: list[TimelineMove] = []
        written_orders: list[Path] = []
        intent_written = False
        try:
            self._write_intent(plan.intent_payload())
            intent_written = True
            if plan.kind == "archive":
                # Save final History positions before the moved rows become visible there
                self._write_plan_orders(
                    plan, plan.queue_ids, plan.history_ids, written_orders
                )
            for move in plan.moves:
                if move.target.root == "history":
                    self.paths.history.mkdir(parents=True, exist_ok=True)
                applied_moves.append(move)
                if not self._apply_move(move):
                    applied_moves.pop()
            if plan.kind == "promote":
                # Do not put a row in Queue order before the file reaches Queue
                self._write_plan_orders(
                    plan, plan.queue_ids, plan.history_ids, written_orders
                )
        except (OSError, RuntimeError, ValueError) as error:
            if not intent_written:
                raise
            rollback_errors: list[str] = []
            for move in reversed(applied_moves):
                try:
                    self._rollback_move(move)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            previous_orders = {
                self.paths.queue_order: plan.previous_queue_ids,
                self.paths.history_order: plan.previous_history_ids,
            }
            for path in (self.paths.queue_order, self.paths.history_order):
                if path not in written_orders:
                    continue
                try:
                    self._write_order(path, list(previous_orders[path]))
                except (OSError, RuntimeError) as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                raise MutationOutcomeUnconfirmed(
                    "timeline rollback failed: " + "; ".join(rollback_errors)
                ) from error
            try:
                self.paths.intent.unlink(missing_ok=True)
            except OSError as rollback_error:
                raise MutationOutcomeUnconfirmed(
                    f"timeline rollback cleanup failed: {rollback_error}"
                ) from error
            raise
        finally:
            self.invalidate_history()

        cleanup_complete = True
        for move in applied_moves:
            if move.backup is None:
                continue
            try:
                self.location_path(move.backup).unlink(missing_ok=True)
            except OSError:
                cleanup_complete = False
        if cleanup_complete:
            # Remove the plan only after every backup is gone
            try:
                self.paths.intent.unlink(missing_ok=True)
            except OSError:
                pass

    def _converge_move(self, move: TimelineMove) -> None:
        source = self.location_path(move.source)
        target = self.location_path(move.target)
        backup = self.location_path(move.backup) if move.backup is not None else None
        target.parent.mkdir(parents=True, exist_ok=True)
        if move.preserve_existing_target:
            if target.exists():
                source.unlink(missing_ok=True)
            elif source.exists():
                os.replace(source, target)
            elif backup is not None and backup.exists():
                os.replace(backup, target)
            else:
                raise RuntimeError(f"pending timeline row is missing: {move.source.name}")
        elif source.exists():
            if backup is not None and target.exists():
                if backup.exists():
                    raise RuntimeError(
                        f"pending timeline backup is ambiguous: {backup.name}"
                    )
                os.replace(target, backup)
            os.replace(source, target)
        elif not target.exists():
            raise RuntimeError(f"pending timeline row is missing: {move.source.name}")
        if backup is not None:
            backup.unlink(missing_ok=True)

    def _converge_plan(self, plan: TimelinePlan) -> None:
        for move in plan.moves:
            self._converge_move(move)
        self._write_order(self.paths.queue_order, list(plan.queue_ids))
        self._write_order(self.paths.history_order, list(plan.history_ids))

    def _recover_current_plan(self) -> None:
        if not self.paths.intent.exists():
            return
        try:
            payload = json.loads(self.paths.intent.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "could not read the pending timeline transaction"
            ) from error
        if not (
            isinstance(payload, dict)
            and payload.get("version") == 3
            and payload.get("operation") == "timeline_plan"
        ):
            raise RuntimeError("pending storage upgrade requires an engine restart")
        self._converge_plan(self._parse_plan(payload))
        self.paths.intent.unlink()
        self.invalidate_history()

    def archive_many(self, paths: list[Path]) -> bool:
        """Move one ordered Queue block to History as one transaction."""
        unique = list(dict.fromkeys(paths))
        if not unique:
            return True
        with self.mutation(timeout=None):
            self._recover_current_plan()
            previous_queue = self.queue_files()
            previous_history = self.history_files()
            archived_names = {path.name for path in unique}
            desired_queue = [
                path for path in previous_queue if path.name not in archived_names
            ]
            retained_history = [
                path for path in previous_history if path.name not in archived_names
            ]
            desired_history = [
                *(self.paths.history / path.name for path in reversed(unique)),
                *retained_history,
            ]
            moves = tuple(
                TimelineMove(
                    TimelineLocation("queue", path.name),
                    TimelineLocation("history", path.name),
                    (
                        TimelineLocation(
                            "history",
                            f".{path.name}.{os.getpid()}.{time.time_ns()}.duplicate",
                        )
                        if (self.paths.history / path.name).exists()
                        else None
                    ),
                )
                for path in unique
            )
            plan = TimelinePlan(
                "archive",
                moves,
                tuple(self.public_id(path) for path in previous_queue),
                tuple(self.public_id(path) for path in previous_history),
                tuple(self.public_id(path) for path in desired_queue),
                tuple(self.public_id(path) for path in desired_history),
            )
            try:
                self._execute_plan(plan)
            except MutationOutcomeUnconfirmed:
                raise
            except (OSError, RuntimeError):
                return False
            return True

    def archive(self, path: Path) -> bool:
        return self.archive_many([path])

    def archive_failed(self, path: Path) -> None:
        with self.mutation(timeout=None):
            self._recover_current_plan()
            self.paths.failed.mkdir(parents=True, exist_ok=True)
            os.replace(path, self.paths.failed / path.name)
            self.save_queue_order()

    def voice_variant(self, source: Path, voice: str) -> Path:
        try:
            filename = SpeechicleFilename.parse(source.name)
            target_name = filename.with_voice(voice).render()
        except ValueError as error:
            raise ValueError(f"invalid speech filename: {source.name}") from error
        return source.with_name(target_name)

    def replace_queue_voice(self, source: Path, voice: str) -> Path:
        with self.mutation(timeout=None):
            self._recover_current_plan()
            target = self.voice_variant(source, voice)
            if target == source:
                return source
            if not source.is_file():
                raise ValueError(f"waiting chunk not found: {source.stem}")
            if target.exists():
                raise RuntimeError(f"voice target already exists: {target.stem}")
            replace_path_with_confirmation(
                source,
                target,
                "Queue voice change",
            )
            return target

    def promote_history(
        self, source: Path, voice: str | None = None
    ) -> tuple[Path, int]:
        """Make one History row Current without changing relative row order."""
        with self.mutation(timeout=None):
            self._recover_current_plan()
            history = self.history_files()
            try:
                selected_index = history.index(source)
            except ValueError as error:
                raise ValueError(f"history chunk not found: {source.stem}") from error
            promoted = history[: selected_index + 1]
            remaining_history = history[selected_index + 1 :]
            previous_queue = self.queue_files()
            selected_source = promoted[-1]
            selected_target = self.paths.queue / selected_source.name
            if voice and voice != self.canonical_filename(selected_source).voice:
                selected_target = self.voice_variant(selected_target, voice)
                if selected_target.exists():
                    raise RuntimeError(
                        f"voice target already exists: {selected_target.stem}"
                    )
            targets = [
                selected_target
                if archived == selected_source
                else self.paths.queue / archived.name
                for archived in promoted
            ]
            playback_order = list(reversed(targets))
            target_names = {path.name for path in targets}
            remaining_queue = [
                path for path in previous_queue if path.name not in target_names
            ]
            moves = tuple(
                TimelineMove(
                    TimelineLocation("history", archived.name),
                    TimelineLocation("queue", queued.name),
                    (
                        TimelineLocation(
                            "history",
                            f".{archived.name}.{os.getpid()}.{time.time_ns()}.duplicate",
                        )
                        if queued.exists()
                        else None
                    ),
                    preserve_existing_target=queued.exists(),
                )
                for archived, queued in zip(promoted, targets)
            )
            plan = TimelinePlan(
                "promote",
                moves,
                tuple(self.public_id(path) for path in previous_queue),
                tuple(self.public_id(path) for path in history),
                tuple(
                    self.public_id(path)
                    for path in [*playback_order, *remaining_queue]
                ),
                tuple(self.public_id(path) for path in remaining_history),
            )
            self._execute_plan(plan)
            return selected_target, len(promoted)

    def select(self, public_id: str, voice: str | None = None) -> TimelineSelection:
        """Move the playback boundary without changing the full row sequence."""
        queue = self.queue_files()
        target = next(
            (path for path in queue if self.public_id(path) == public_id),
            None,
        )
        if target is not None:
            target_index = queue.index(target)
            origin: Literal["current", "waiting"] = (
                "current" if target_index == 0 else "waiting"
            )
            current_voice = self.canonical_filename(target).voice
            if origin == "current" and (voice is None or voice == current_voice):
                return TimelineSelection(target, origin, False, 0)

            original = target
            voice_changed = voice is not None and voice != current_voice
            if voice_changed:
                target = self.replace_queue_voice(original, voice)
            older = queue[:target_index]
            try:
                if not self.archive_many(older):
                    raise RuntimeError("could not archive older waiting chunks")
            except (OSError, RuntimeError, ValueError) as error:
                outcome_unconfirmed = isinstance(error, MutationOutcomeUnconfirmed)
                if voice_changed:
                    try:
                        self.replace_queue_voice(target, current_voice)
                    except (OSError, RuntimeError, ValueError):
                        outcome_unconfirmed = True
                if outcome_unconfirmed:
                    raise MutationOutcomeUnconfirmed(
                        "play command result was unconfirmed"
                    ) from error
                raise
            return TimelineSelection(target, origin, True, len(older))

        history_source = self.find(self.paths.history, public_id)
        if history_source is None:
            raise ValueError(f"chunk not found: {public_id}")
        selected, moved_count = self.promote_history(history_source, voice)
        return TimelineSelection(selected, "history", True, moved_count)

    def delete_history(self, public_id: str) -> Path | None:
        with self.mutation(timeout=None):
            self._recover_current_plan()
            if self.find(self.paths.queue, public_id) is not None:
                raise ValueError(f"history chunk is active: {public_id}")
            history_item = self.find(self.paths.history, public_id)
            if history_item is None:
                return None
            history_item.unlink()
            self.invalidate_history()
            return history_item

    def reorder_history(self, public_id: str, before_id: str | None, limit: int) -> Path:
        with self.mutation(timeout=None):
            self._recover_current_plan()
            ordered = self.history_files()
            source = next(
                (path for path in ordered if self.public_id(path) == public_id), None
            )
            if source is None:
                raise ValueError(f"history chunk not found: {public_id}")
            if before_id == public_id:
                return source
            ordered.remove(source)
            if before_id is None:
                # Move to the bottom of visible History, not behind older hidden rows
                ordered.insert(min(limit - 1, len(ordered)), source)
            else:
                destination = next(
                    (path for path in ordered if self.public_id(path) == before_id),
                    None,
                )
                if destination is None:
                    raise ValueError(f"history destination not found: {before_id}")
                ordered.insert(ordered.index(destination), source)
            self.save_history_order(ordered)
            self.invalidate_history()
            return source

    def waiting_source(self, public_id: str) -> tuple[list[Path], Path]:
        """Find a Waiting row; Queue's first row is Current, not Waiting."""
        ordered = self.queue_files()
        source = next(
            (path for path in ordered if self.public_id(path) == public_id), None
        )
        if source is None or (ordered and source == ordered[0]):
            raise ValueError(f"waiting chunk not found: {public_id}")
        return ordered, source

    def reorder_waiting(
        self, public_id: str, before_id: str | None
    ) -> tuple[Path, Path]:
        with self.mutation(timeout=None):
            self._recover_current_plan()
            ordered, source = self.waiting_source(public_id)
            current = ordered[0]
            if before_id == public_id:
                return current, source
            ordered.remove(source)
            if before_id is None:
                ordered.append(source)
            else:
                destination = next(
                    (
                        path
                        for path in ordered
                        if self.public_id(path) == before_id and path != current
                    ),
                    None,
                )
                if destination is None:
                    raise ValueError(f"waiting destination not found: {before_id}")
                ordered.insert(ordered.index(destination), source)
            self.save_queue_order(ordered)
            return current, source

    def _legacy_catalog(self) -> IdentityCatalog | None:
        try:
            return load_catalog(self.paths.legacy_identity_index)
        except (TypeError, ValueError) as error:
            raise RuntimeError(str(error)) from error

    def _apply_old_identity_migration(self, payload: dict[str, object]) -> None:
        try:
            migration = migration_from_intent(payload)
        except (TypeError, ValueError) as error:
            raise RuntimeError("invalid pending identity migration") from error
        directories = {
            "queue": self.paths.queue,
            "spoken": self.paths.history,
            "failed": self.paths.failed,
        }
        for removal in migration.removals:
            (directories[removal.directory] / removal.name).unlink(missing_ok=True)
        for move in migration.moves:
            source = directories[move.source_directory] / move.source
            target = directories[move.target_directory] / move.target
            if source == target or (target.exists() and not source.exists()):
                continue
            if source.exists() and not target.exists():
                os.replace(source, target)
                continue
            raise RuntimeError(f"identity migration could not reconcile {source.name}")
        write_catalog(self.paths.legacy_identity_index, migration.catalog)
        self._write_order(self.paths.queue_order, list(migration.queue_ids))
        self._write_order(self.paths.history_order, list(migration.history_ids))

    @staticmethod
    def _legacy_order_version(payload: dict[object, object]) -> Literal[1, 2]:
        value = payload.get("order_version", 1)
        if value not in {1, 2}:
            raise RuntimeError("invalid pending timeline order version")
        return value

    @staticmethod
    def _parse_legacy_ids(
        value: object, label: str, order_version: Literal[1, 2]
    ) -> tuple[str, ...]:
        valid = isinstance(value, list) and all(
            is_public_id(item)
            if order_version == 2
            else _safe_timeline_filename(item)
            for item in value
        )
        if not valid:
            raise RuntimeError(f"invalid {label}")
        if len(value) != len(set(value)):
            raise RuntimeError(f"duplicate {label}")
        return tuple(value)

    @staticmethod
    def _parse_legacy_filename(value: object, label: str) -> str:
        if not _safe_timeline_filename(value):
            raise RuntimeError(f"invalid {label}")
        return value

    def _parse_legacy_moves(
        self, value: object, kind: Literal["archive", "promote"]
    ) -> tuple[TimelineMove, ...]:
        if not isinstance(value, list) or not value:
            raise RuntimeError("invalid pending timeline moves")
        moves: list[TimelineMove] = []
        sources: set[TimelineLocation] = set()
        targets: set[TimelineLocation] = set()
        backups: set[TimelineLocation] = set()
        for raw_move in value:
            if not isinstance(raw_move, dict):
                raise RuntimeError("invalid pending timeline move")
            source_name = self._parse_legacy_filename(
                raw_move.get("source"), "pending timeline source"
            )
            target_name = self._parse_legacy_filename(
                raw_move.get("target"), "pending timeline target"
            )
            backup_name = raw_move.get("backup")
            backup = (
                None
                if backup_name is None
                else TimelineLocation(
                    "history",
                    self._parse_legacy_filename(
                        backup_name, "pending timeline backup"
                    ),
                )
            )
            if backup is not None and (
                not backup.name.startswith(f".{source_name}.")
                or not backup.name.endswith(".duplicate")
            ):
                raise RuntimeError("invalid pending timeline backup")
            source = TimelineLocation(
                "queue" if kind == "archive" else "history", source_name
            )
            target = TimelineLocation(
                "history" if kind == "archive" else "queue", target_name
            )
            sequence = strict_sequence(source_name)
            if sequence is None or strict_sequence(target_name) != sequence:
                raise RuntimeError("pending timeline move changes the storage sequence")
            renamed_source = TimelineLocation("queue", source_name)
            if (
                kind == "promote"
                and not self.location_path(source).exists()
                and not self.location_path(target).exists()
                and (backup is None or not self.location_path(backup).exists())
                and self.location_path(renamed_source).exists()
            ):
                source = renamed_source
            if source in sources or target in targets:
                raise RuntimeError("pending timeline contains a duplicate move")
            if backup is not None and (
                backup in backups
                or backup in sources
                or backup in targets
                or backup in {source, target}
            ):
                raise RuntimeError("pending timeline contains a duplicate backup")
            sources.add(source)
            targets.add(target)
            if backup is not None:
                backups.add(backup)
            moves.append(
                TimelineMove(
                    source,
                    target,
                    backup,
                    preserve_existing_target=kind == "promote" and backup is not None,
                )
            )
        if backups & (sources | targets):
            raise RuntimeError("pending timeline backup overlaps a moved row")
        return tuple(moves)

    def _queue_ids_for_legacy_recovery(
        self,
        order_version: Literal[1, 2],
        catalog: IdentityCatalog | None,
    ) -> tuple[str, ...]:
        """Read Queue without processing the same saved upgrade again."""
        paths = sorted(self.paths.queue.glob("*.txt"), key=self.queue_sort_key)
        if order_version == 1:
            return tuple(path.stem for path in paths)
        if catalog is None:
            raise RuntimeError("version-2 timeline recovery requires the identity catalog")
        live = {
            public_id: path
            for path in paths
            if (sequence := strict_sequence(path.name)) is not None
            and (public_id := catalog.public_id(sequence)) is not None
        }
        try:
            payload = json.loads(self.paths.queue_order.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            saved_ids: list[str] = []
        else:
            raw_ids = payload.get("ids") if isinstance(payload, dict) else None
            saved_ids = (
                raw_ids
                if isinstance(payload, dict)
                and payload.get("version") == 2
                and isinstance(raw_ids, list)
                and all(is_public_id(item) for item in raw_ids)
                else []
            )
        ordered = list(
            dict.fromkeys(item for item in saved_ids if item in live)
        )
        ordered.extend(item for item in live if item not in ordered)
        return tuple(ordered)

    def _adapt_legacy_plan(
        self,
        payload: dict[object, object],
        catalog: IdentityCatalog | None,
    ) -> _LegacyTimelinePlan:
        operation = payload.get("operation")
        order_version = self._legacy_order_version(payload)
        if order_version == 2 and catalog is None:
            raise RuntimeError("version-2 timeline recovery requires the identity catalog")
        if operation == "archive":
            name = self._parse_legacy_filename(
                payload.get("name"), "pending archive filename"
            )
            previous_history = self._parse_legacy_ids(
                payload.get("previous_history_ids"),
                "pending previous History IDs",
                order_version,
            )
            desired_history = self._parse_legacy_ids(
                payload.get("desired_history_ids"),
                "pending final History IDs",
                order_version,
            )
            source = TimelineLocation("queue", name)
            target = TimelineLocation("history", name)
            if self.location_path(source).exists():
                moves: tuple[TimelineMove, ...] = ()
                final_history = previous_history
            elif self.location_path(target).exists():
                moves = (TimelineMove(source, target),)
                final_history = desired_history
            else:
                raise RuntimeError(f"pending archive row is missing: {name}")
            queue_ids = self._queue_ids_for_legacy_recovery(order_version, catalog)
            return _LegacyTimelinePlan(
                "archive",
                moves,
                queue_ids,
                previous_history,
                queue_ids,
                final_history,
                order_version,
            )
        if operation not in {"archive_batch", "promote"}:
            raise RuntimeError("unknown pending timeline transaction")
        kind: Literal["archive", "promote"] = (
            "archive" if operation == "archive_batch" else "promote"
        )
        return _LegacyTimelinePlan(
            kind,
            self._parse_legacy_moves(payload.get("moves"), kind),
            self._parse_legacy_ids(
                payload.get("previous_queue_ids", []),
                "pending previous Queue IDs",
                order_version,
            ),
            self._parse_legacy_ids(
                payload.get("previous_history_ids", []),
                "pending previous History IDs",
                order_version,
            ),
            self._parse_legacy_ids(
                payload.get("queue_ids"), "pending final Queue IDs", order_version
            ),
            self._parse_legacy_ids(
                payload.get("history_ids"),
                "pending final History IDs",
                order_version,
            ),
            order_version,
        )

    def _parse_legacy_v2_plan(
        self, payload: object, catalog: IdentityCatalog | None
    ) -> _LegacyTimelinePlan:
        if catalog is None:
            raise RuntimeError("version-2 timeline recovery requires the identity catalog")
        expected_fields = {
            "version",
            "operation",
            "moves",
            "previous_queue_ids",
            "previous_history_ids",
            "queue_ids",
            "history_ids",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise RuntimeError("invalid pending timeline plan")
        if payload["version"] != 2 or payload["operation"] != "timeline_plan":
            raise RuntimeError("unsupported pending timeline plan")
        raw_moves = payload["moves"]
        if not isinstance(raw_moves, list) or not raw_moves:
            raise RuntimeError("invalid pending timeline moves")
        kinds: set[Literal["archive", "promote"]] = set()
        moves: list[TimelineMove] = []
        source_sequences: set[int] = set()
        for raw_move in raw_moves:
            expected_move_fields = {
                "source",
                "target",
                "backup",
                "preserve_existing_target",
            }
            if not isinstance(raw_move, dict) or set(raw_move) != expected_move_fields:
                raise RuntimeError("invalid pending timeline move")
            source = self._parse_location(raw_move["source"], "timeline source")
            target = self._parse_location(raw_move["target"], "timeline target")
            roots = (source.root, target.root)
            if roots == ("queue", "history"):
                kinds.add("archive")
            elif roots == ("history", "queue"):
                kinds.add("promote")
            else:
                raise RuntimeError("invalid timeline move direction")
            source_sequence = strict_sequence(source.name)
            if (
                source_sequence is None
                or strict_sequence(target.name) != source_sequence
                or source_sequence in source_sequences
            ):
                raise RuntimeError("invalid legacy timeline move")
            source_sequences.add(source_sequence)
            raw_backup = raw_move["backup"]
            backup = (
                None
                if raw_backup is None
                else self._parse_location(raw_backup, "timeline backup")
            )
            preserve = raw_move["preserve_existing_target"]
            if not isinstance(preserve, bool):
                raise RuntimeError("invalid timeline duplicate policy")
            moves.append(TimelineMove(source, target, backup, preserve))
        if len(kinds) != 1:
            raise RuntimeError("timeline plan mixes move directions")
        plan = _LegacyTimelinePlan(
            kinds.pop(),
            tuple(moves),
            self._parse_ids(payload["previous_queue_ids"], "previous Queue IDs"),
            self._parse_ids(
                payload["previous_history_ids"], "previous History IDs"
            ),
            self._parse_ids(payload["queue_ids"], "final Queue IDs"),
            self._parse_ids(payload["history_ids"], "final History IDs"),
            2,
        )
        previous_queue = set(plan.previous_queue_ids)
        previous_history = set(plan.previous_history_ids)
        final_queue = set(plan.queue_ids)
        final_history = set(plan.history_ids)
        moved_ids = (
            previous_queue - final_queue
            if plan.kind == "archive"
            else previous_history - final_history
        )
        catalog_ids = {catalog.public_id(sequence) for sequence in source_sequences}
        if None in catalog_ids or moved_ids != catalog_ids:
            raise RuntimeError("timeline move files contradict its orders")
        return plan

    def _converge_legacy_plan(self, plan: _LegacyTimelinePlan) -> None:
        for move in plan.moves:
            self._converge_move(move)
        self._write_order(
            self.paths.queue_order, list(plan.queue_ids), plan.order_version
        )
        self._write_order(
            self.paths.history_order, list(plan.history_ids), plan.order_version
        )

    @staticmethod
    def _embed_sections(
        migration: EmbedPublicIdsMigration,
    ) -> tuple[
        tuple[Literal["queue", "spoken", "failed"], tuple[EmbedPublicIdsFile, ...]],
        ...,
    ]:
        return (
            ("queue", migration.queue_files),
            ("spoken", migration.history_files),
            ("failed", migration.failed_files),
        )

    def _migration_root(self, root: str) -> Path:
        return {
            "queue": self.paths.queue,
            "spoken": self.paths.history,
            "failed": self.paths.failed,
        }[root]

    def _validate_embed_inventory(
        self, migration: EmbedPublicIdsMigration, *, final: bool
    ) -> None:
        """Check planned file locations and hashes before and after an upgrade."""
        actual = {
            (root, path.name): path
            for root in ("queue", "spoken", "failed")
            for path in self._migration_root(root).glob("*.txt")
        }
        allowed: set[tuple[str, str]] = set()
        expected_final: dict[tuple[str, str], str] = {}
        for target_root, files in self._embed_sections(migration):
            for item in files:
                source_key = (item.source_root, item.source_name)
                target_key = (target_root, item.target_name)
                allowed.update((source_key, target_key))
                expected_final[target_key] = item.sha256
                present = [key for key in {source_key, target_key} if key in actual]
                if final:
                    if present != [target_key] and not (
                        source_key == target_key and present == [source_key]
                    ):
                        raise RuntimeError(
                            "embedded identity target inventory is incomplete"
                        )
                elif len(present) != 1:
                    raise RuntimeError("embedded identity migration state is ambiguous")
                for key in present:
                    if _file_hash(actual[key]) != item.sha256:
                        raise RuntimeError(f"speech file hash changed: {key[1]}")
        for removal in migration.removals:
            removal_key = (removal.source_root, removal.source_name)
            allowed.add(removal_key)
            if (
                removal_key in actual
                and _file_hash(actual[removal_key]) != removal.sha256
            ):
                raise RuntimeError(f"speech file hash changed: {removal.source_name}")
            if final and removal_key in actual:
                raise RuntimeError("embedded replay duplicate still exists")
        if final:
            if set(actual) != set(expected_final):
                raise RuntimeError(
                    "embedded identity final inventory has undeclared files"
                )
        elif not set(actual) <= allowed:
            raise RuntimeError("embedded identity source inventory has undeclared files")

    def _converge_embed(self, migration: EmbedPublicIdsMigration) -> None:
        self._validate_embed_inventory(migration, final=False)
        for removal in migration.removals:
            (self._migration_root(removal.source_root) / removal.source_name).unlink(
                missing_ok=True
            )
        for target_root, files in self._embed_sections(migration):
            target_directory = self._migration_root(target_root)
            target_directory.mkdir(parents=True, exist_ok=True)
            for item in files:
                source = self._migration_root(item.source_root) / item.source_name
                target = target_directory / item.target_name
                if source == target or (target.exists() and not source.exists()):
                    continue
                if source.exists() and not target.exists():
                    os.replace(source, target)
                    continue
                raise RuntimeError(
                    f"could not converge embedded identity {item.source_name}"
                )
        self._validate_embed_inventory(migration, final=True)
        self._write_order(self.paths.history_order, list(migration.history_ids))
        self._write_order(self.paths.queue_order, list(migration.queue_ids))

    def _recover_any_intent(self) -> str | None:
        if not self.paths.intent.exists():
            return None
        try:
            payload = json.loads(self.paths.intent.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "could not read the pending timeline transaction"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError("invalid pending timeline transaction")
        operation = payload.get("operation")
        version = payload.get("version")
        if version == 1 and operation == "identity_migration":
            self._apply_old_identity_migration(payload)
        elif version == 3 and operation == "embed_public_ids":
            try:
                migration = embed_public_ids_from_intent(payload)
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    "invalid pending embedded identity migration"
                ) from error
            self._converge_embed(migration)
            inventory = self.canonical_inventory()
            self._normalize_orders()
            self._prepare_sequence_counter(inventory)
            self.paths.legacy_identity_index.unlink(missing_ok=True)
        elif version == 3 and operation == "timeline_plan":
            self._converge_plan(self._parse_plan(payload))
        elif version in {1, 2}:
            catalog = self._legacy_catalog()
            plan = (
                self._parse_legacy_v2_plan(payload, catalog)
                if version == 2
                else self._adapt_legacy_plan(payload, catalog)
            )
            self._converge_legacy_plan(plan)
        else:
            raise RuntimeError("invalid pending timeline transaction")
        self.paths.intent.unlink()
        self.invalidate_history()
        if not isinstance(operation, str):
            raise RuntimeError("invalid pending timeline transaction")
        return operation

    @staticmethod
    def _is_canonical_name(name: str) -> bool:
        try:
            SpeechicleFilename.parse(name)
        except ValueError:
            return False
        return True

    def _all_files_canonical(self) -> bool:
        return all(
            self._is_canonical_name(path.name)
            for directory in (
                self.paths.queue,
                self.paths.history,
                self.paths.failed,
            )
            for path in directory.glob("*.txt")
        )

    def _order_is_strict(self, path: Path) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        try:
            self._strict_order_ids(payload, path.name)
        except RuntimeError:
            return False
        return True

    def _normalized_order_ids(
        self, directory: Path, order_path: Path, *, history: bool
    ) -> list[str]:
        inventory = {
            self.public_id(path): path for path in directory.glob("*.txt")
        }
        try:
            payload = json.loads(order_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            saved: list[str] = []
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid speech order: {order_path.name}") from error
        else:
            saved = self._strict_order_ids(payload, order_path.name)
        ordered = list(
            dict.fromkeys(public_id for public_id in saved if public_id in inventory)
        )
        missing = sorted(
            (path for public_id, path in inventory.items() if public_id not in ordered),
            key=self.history_sort_key if history else self.queue_sort_key,
            reverse=history,
        )
        missing_ids = [self.public_id(path) for path in missing]
        return [*missing_ids, *ordered] if history else [*ordered, *missing_ids]

    def _normalize_orders(self) -> None:
        queue_ids = self._normalized_order_ids(
            self.paths.queue, self.paths.queue_order, history=False
        )
        history_ids = self._normalized_order_ids(
            self.paths.history, self.paths.history_order, history=True
        )
        self._write_order(self.paths.history_order, history_ids)
        self._write_order(self.paths.queue_order, queue_ids)
        if set(queue_ids) != {
            self.public_id(path) for path in self.paths.queue.glob("*.txt")
        } or len(queue_ids) != len(set(queue_ids)):
            raise RuntimeError("Queue order does not cover canonical storage")
        if set(history_ids) != {
            self.public_id(path) for path in self.paths.history.glob("*.txt")
        } or len(history_ids) != len(set(history_ids)):
            raise RuntimeError("History order does not cover canonical storage")

    def prepare(self, instance_lock: HeldInstanceLock) -> str | None:
        """Recover old storage and validate canonical files under the engine lock."""
        if not instance_lock.held:
            raise RuntimeError("timeline preparation requires the held engine instance lock")
        self.paths.queue.mkdir(parents=True, exist_ok=True)
        self.paths.history.mkdir(parents=True, exist_ok=True)
        self.paths.failed.mkdir(parents=True, exist_ok=True)
        with self.mutation(timeout=None):
            recovered = self._recover_any_intent()
            if recovered == "embed_public_ids":
                # Embed recovery already checked inventory, orders, and the counter
                return recovered
            needs_embed = not self._all_files_canonical()
            if not needs_embed:
                try:
                    self.canonical_inventory()
                except RuntimeError:
                    needs_embed = True
                needs_embed = needs_embed or not (
                    self._order_is_strict(self.paths.queue_order)
                    and self._order_is_strict(self.paths.history_order)
                )
            if needs_embed:
                try:
                    migration = plan_embed_public_ids(
                        self.paths.queue,
                        self.paths.history,
                        self.paths.failed,
                        self.paths.queue_order,
                        self.paths.history_order,
                        self.paths.legacy_identity_index,
                        self.default_voice,
                    )
                except (TypeError, ValueError) as error:
                    raise RuntimeError(str(error)) from error
                if migration is None:
                    raise RuntimeError(
                        "storage repair did not produce an embed migration"
                    )
                self._write_intent(migration.intent_payload())
                self._converge_embed(migration)
            inventory = self.canonical_inventory()
            self._normalize_orders()
            self._prepare_sequence_counter(inventory)
            self.paths.legacy_identity_index.unlink(missing_ok=True)
            self.paths.intent.unlink(missing_ok=True)
            self.invalidate_history()
            return recovered
