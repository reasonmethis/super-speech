"""Validate and normalize timeline mutations before they reach the engine loop."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from speechicle_identity import is_public_id

REQUEST_ID_PATTERN = re.compile(r"[a-f0-9]{24}")
VOICE_PATTERN = re.compile(r"[ab][fm]_[a-z0-9_]+")
MUTATION_TYPES = frozenset({"play", "move", "archive", "delete", "clear"})


@dataclass(frozen=True)
class PlayMutation:
    """Select one Speechicle, optionally with a different voice."""

    request_id: str
    id: str
    voice: str | None
    type: Literal["play"] = field(default="play", init=False)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "request_id": self.request_id,
            "type": self.type,
            "id": self.id,
        }
        if self.voice is not None:
            payload["voice"] = self.voice
        return payload


@dataclass(frozen=True)
class MoveMutation:
    """Move one Speechicle within Waiting or History."""

    request_id: str
    section: Literal["waiting", "history"]
    id: str
    before_id: str | None
    type: Literal["move"] = field(default="move", init=False)

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "type": self.type,
            "section": self.section,
            "id": self.id,
            "before_id": self.before_id,
        }


@dataclass(frozen=True)
class ArchiveMutation:
    """Move one Waiting Speechicle to History."""

    request_id: str
    id: str
    type: Literal["archive"] = field(default="archive", init=False)

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "type": self.type,
            "id": self.id,
        }


@dataclass(frozen=True)
class DeleteMutation:
    """Delete one History Speechicle."""

    request_id: str
    id: str
    type: Literal["delete"] = field(default="delete", init=False)

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "type": self.type,
            "id": self.id,
        }


@dataclass(frozen=True)
class ClearMutation:
    """Move Current and every Waiting Speechicle to History."""

    request_id: str
    type: Literal["clear"] = field(default="clear", init=False)

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "type": self.type,
        }


MutationRequest: TypeAlias = (
    PlayMutation | MoveMutation | ArchiveMutation | DeleteMutation | ClearMutation
)


def validate_request_id(value: object) -> str:
    if not isinstance(value, str) or REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid mutation request ID")
    return value


def _validate_id(value: object, label: str = "Speechicle ID") -> str:
    if not is_public_id(value):
        raise ValueError(f"invalid {label}")
    return value


def _validate_voice(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or VOICE_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid Kokoro voice")
    return value


def parse_durable_mutation(payload: object) -> MutationRequest:
    """Parse the flat JSON object stored in a MUTATION request file."""
    if not isinstance(payload, dict):
        raise ValueError("mutation must be an object")
    mutation_type = payload.get("type")
    if mutation_type not in MUTATION_TYPES:
        raise ValueError("invalid mutation type")
    request_id = validate_request_id(payload.get("request_id"))

    if mutation_type == "play":
        _reject_extra_fields(payload, {"request_id", "type", "id", "voice"})
        return PlayMutation(
            request_id=request_id,
            id=_validate_id(payload.get("id")),
            voice=_validate_voice(payload.get("voice")),
        )
    if mutation_type == "move":
        _reject_extra_fields(
            payload, {"request_id", "type", "section", "id", "before_id"}
        )
        section = payload.get("section")
        if section != "waiting" and section != "history":
            raise ValueError("invalid move section")
        before_value = payload.get("before_id")
        before_id = (
            None
            if before_value is None
            else _validate_id(before_value, "destination Speechicle ID")
        )
        return MoveMutation(
            request_id=request_id,
            section=section,
            id=_validate_id(payload.get("id")),
            before_id=before_id,
        )
    if mutation_type == "archive":
        _reject_extra_fields(payload, {"request_id", "type", "id"})
        return ArchiveMutation(
            request_id=request_id,
            id=_validate_id(payload.get("id")),
        )
    if mutation_type == "delete":
        _reject_extra_fields(payload, {"request_id", "type", "id"})
        return DeleteMutation(
            request_id=request_id,
            id=_validate_id(payload.get("id")),
        )

    _reject_extra_fields(payload, {"request_id", "type"})
    return ClearMutation(request_id=request_id)


def parse_cli_mutation(payload: object, request_id: str) -> MutationRequest:
    """Normalize the camelCase object accepted by the private mutate command."""
    if not isinstance(payload, dict):
        raise ValueError("mutation must be an object")
    normalized = dict(payload)
    if "requestId" in normalized or "request_id" in normalized:
        raise ValueError("mutate assigns the request ID")
    if "beforeId" in normalized:
        if "before_id" in normalized:
            raise ValueError("duplicate move destination")
        normalized["before_id"] = normalized.pop("beforeId")
    normalized["request_id"] = request_id
    return parse_durable_mutation(normalized)


def _reject_extra_fields(payload: dict[object, object], allowed: set[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        names = ", ".join(sorted(str(name) for name in extra))
        raise ValueError(f"unexpected mutation field(s): {names}")
