"""Archive record types and their JSON Lines encoding."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

RECORDER_TOPIC = "_recorder"
_MARKER_FIELDS = frozenset({"received_at", "topic", "event"})


class InvalidRecordError(ValueError):
    pass


@dataclass(frozen=True)
class Message:
    received_at: datetime
    topic: str
    payload: str


@dataclass(frozen=True)
class BadPayload:
    received_at: datetime
    topic: str
    payload_b64: str
    error: str


@dataclass(frozen=True)
class Marker:
    received_at: datetime
    event: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clash = _MARKER_FIELDS & self.details.keys()
        if clash:
            raise ValueError(f"marker details may not use {sorted(clash)}")


type Record = Message | BadPayload | Marker


def format_timestamp(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    if at.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {text!r}")
    return at.astimezone(UTC)


def encode(record: Record) -> str:
    data: dict[str, Any] = {"received_at": format_timestamp(record.received_at)}
    match record:
        case Message():
            data |= {"topic": record.topic, "payload": record.payload}
        case BadPayload():
            data |= {
                "topic": record.topic,
                "payload_b64": record.payload_b64,
                "error": record.error,
            }
        case Marker():
            data |= {"topic": RECORDER_TOPIC, "event": record.event, **record.details}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def decode(line: str) -> Record:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise InvalidRecordError(f"not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise InvalidRecordError("record is not a JSON object")
    try:
        received_at = parse_timestamp(_text(data, "received_at"))
        topic = _text(data, "topic")
        if topic == RECORDER_TOPIC:
            return Marker(received_at, _text(data, "event"), data)
        if data.keys() == {"payload"}:
            return Message(received_at, topic, _text(data, "payload"))
        if data.keys() == {"payload_b64", "error"}:
            return BadPayload(received_at, topic, _text(data, "payload_b64"), _text(data, "error"))
    except (KeyError, ValueError) as exc:
        raise InvalidRecordError(str(exc)) from None
    raise InvalidRecordError(f"unknown record fields: {sorted(data)}")


def _text(data: dict[str, Any], key: str) -> str:
    """Remove `key` from `data` and return it, insisting on a string."""
    value = data.pop(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value
