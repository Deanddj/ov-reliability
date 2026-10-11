"""Turning archive records into one batch of rows to store."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ov_reliability.collector.records import BadPayload, Marker, Message, Record
from ov_reliability.kv6.parse import Mutation, StopEvent, TripKey, mentions_owner, parse_message
from ov_reliability.kv6.payload import BadMessageError, decode_payload

_LOADED_TOPICS = ("/KV6posinfo", "/KV17cvlinfo")


@dataclass(frozen=True)
class SeenSpan:
    trip: TripKey
    first_at: datetime
    last_at: datetime


@dataclass(frozen=True)
class MarkerRow:
    at: datetime
    event: str
    details: dict[str, Any]


@dataclass(frozen=True)
class BadMessage:
    received_at: datetime
    topic: str
    reason: str


@dataclass(frozen=True)
class Batch:
    events: tuple[StopEvent, ...]
    seen: tuple[SeenSpan, ...]
    mutations: tuple[Mutation, ...]
    markers: tuple[MarkerRow, ...]
    bad: tuple[BadMessage, ...]

    @property
    def days(self) -> set[date]:
        trips = [item.trip for item in self.events]
        trips += [item.trip for item in self.seen]
        trips += [item.trip for item in self.mutations]
        return {trip.operating_day for trip in trips}


def window_start(today: date, window_days: int) -> date:
    """The oldest operating day kept in the database."""
    return today - timedelta(days=window_days)


def build_batch(
    records: Iterable[Record],
    owners: Collection[str],
    today: date,
    window_days: int,
    floor: date | None,
) -> Batch:
    oldest = window_start(today, window_days)
    newest = today + timedelta(days=1)
    events: list[StopEvent] = []
    seen: dict[TripKey, SeenSpan] = {}
    mutations: list[Mutation] = []
    markers: list[MarkerRow] = []
    bad: list[BadMessage] = []

    def allowed_days(record: Message | BadPayload, days: set[date]) -> set[date]:
        allowed: set[date] = set()
        for day in sorted(days):
            if floor is not None and day < floor:
                # Replay keeps the days before its floor as they are (decision 074).
                continue
            if not oldest <= day <= newest:
                reason = f"operating day {day} is outside the window"
                bad.append(BadMessage(record.received_at, record.topic, reason))
                continue
            allowed.add(day)
        return allowed

    for record in records:
        if isinstance(record, Marker):
            markers.append(MarkerRow(record.received_at, record.event, record.details))
            continue
        if not record.topic.endswith(_LOADED_TOPICS):
            continue
        if isinstance(record, Message) and not mentions_owner(record.payload, owners):
            continue
        try:
            root = decode_payload(record)
        except BadMessageError as exc:
            bad.append(BadMessage(record.received_at, record.topic, str(exc)))
            continue
        parsed = parse_message(record.topic, root, record.received_at, owners)
        bad.extend(BadMessage(record.received_at, record.topic, reason) for reason in parsed.bad)
        trips = [item.trip for item in parsed.events]
        trips += [item.trip for item in parsed.seen]
        trips += [item.trip for item in parsed.mutations]
        days = allowed_days(record, {trip.operating_day for trip in trips})
        events.extend(event for event in parsed.events if event.trip.operating_day in days)
        mutations.extend(item for item in parsed.mutations if item.trip.operating_day in days)
        for item in parsed.seen:
            if item.trip.operating_day not in days:
                continue
            span = seen.get(item.trip)
            if span is None:
                seen[item.trip] = SeenSpan(item.trip, item.at, item.at)
            else:
                seen[item.trip] = SeenSpan(
                    item.trip, min(span.first_at, item.at), max(span.last_at, item.at)
                )
    return Batch(tuple(events), tuple(seen.values()), tuple(mutations), tuple(markers), tuple(bad))
