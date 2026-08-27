"""Validate and normalize timeline mutations before they reach the engine loop."""

from __future__ import annotations

from dataclasses import dataclass
import re

from speechicle_identity import is_public_id


REQUEST_ID_PATTERN = re.compile(r"[a-f0-9]{24}")
VOICE_PATTERN = re.compile(r"[ab][fm]_[a-z0-9_]+")
MUTATION_TYPES = frozenset({"play", "move", "archive", "delete", "clear"})
MOVE_SECTIONS = frozenset({"waiting", "history"})


@dataclass(frozen=True)
class MutationRequest:
    """One validated timeline change in the engine's durable wire format."""

    request_id: str
    type: str
    id: str | None = None
    voice: str | None = None
    section: str | None = None
    before_id: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "request_id": self.request_id,
            "type": self.type,
        }
        if self.id is not None:
            payload["id"] = self.id
        if self.voice is not None:
            payload["voice"] = self.voice
        if self.section is not None:
            payload["section"] = self.section
            payload["before_id"] = self.before_id
        return payload


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
        allowed = {"request_id", "type", "id", "voice"}
        _reject_extra_fields(payload, allowed)
        return MutationRequest(
            request_id=request_id,
            type="play",
            id=_validate_id(payload.get("id")),
            voice=_validate_voice(payload.get("voice")),
        )
    if mutation_type == "move":
        allowed = {"request_id", "type", "section", "id", "before_id"}
        _reject_extra_fields(payload, allowed)
        section = payload.get("section")
        if section not in MOVE_SECTIONS:
            raise ValueError("invalid move section")
        before_value = payload.get("before_id")
        before_id = (
            None
            if before_value is None
            else _validate_id(before_value, "destination Speechicle ID")
        )
        return MutationRequest(
            request_id=request_id,
            type="move",
            section=section,
            id=_validate_id(payload.get("id")),
            before_id=before_id,
        )
    if mutation_type in {"archive", "delete"}:
        allowed = {"request_id", "type", "id"}
        _reject_extra_fields(payload, allowed)
        return MutationRequest(
            request_id=request_id,
            type=mutation_type,
            id=_validate_id(payload.get("id")),
        )

    _reject_extra_fields(payload, {"request_id", "type"})
    return MutationRequest(request_id=request_id, type="clear")


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
