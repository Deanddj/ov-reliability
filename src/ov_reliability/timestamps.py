"""The one timestamp format used in everything the services write: UTC, milliseconds, "Z"."""

from datetime import UTC, datetime


def format_timestamp(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    if at.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {text!r}")
    return at.astimezone(UTC)
