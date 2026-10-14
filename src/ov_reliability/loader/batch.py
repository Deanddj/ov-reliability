"""Turning archive records into one batch of rows to store."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from ov_reliability.collector.records import BadPayload, Marker, Message, Record
from ov_reliability.kv6.parse import Mutation, StopEvent, TripKey, mentions_owner, parse_message
from ov_reliability.kv6.payload import BadMessageError, decode_payload
from ov_reliability.siri.parse import parse_estimated_timetable

type Source = Literal["KV6", "KV17", "SIRI"]

_KV_TOPICS = ("/KV6posinfo", "/KV17cvlinfo")
_SIRI_TOPIC = "/EstimatedTimetableDelivery"


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
    events: tuple[tuple[StopEvent, Source], ...]
    seen: tuple[SeenSpan, ...]
    mutations: tuple[tuple[Mutation, Source], ...]
    markers: tuple[MarkerRow, ...]
    bad: tuple[BadMessage, ...]

    @property
    def days(self) -> set[date]:
        trips = [item.trip for item, _ in self.events]
        trips += [item.trip for item in self.seen]
        trips += [item.trip for item, _ in self.mutations]
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
    events: list[tuple[StopEvent, Source]] = []
    seen: dict[TripKey, SeenSpan] = {}
    mutations: list[tuple[Mutation, Source]] = []
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
        siri = record.topic.endswith(_SIRI_TOPIC)
        if not siri and not record.topic.endswith(_KV_TOPICS):
            continue
        if not siri and isinstance(record, Message) and not mentions_owner(record.payload, owners):
            continue
        try:
            root = decode_payload(record)
        except BadMessageError as exc:
            bad.append(BadMessage(record.received_at, record.topic, str(exc)))
            continue
        if siri:
            parsed = parse_estimated_timetable(root, record.received_at, owners)
        else:
            parsed = parse_message(record.topic, root, record.received_at, owners)
        bad.extend(BadMessage(record.received_at, record.topic, reason) for reason in parsed.bad)
        trips = [item.trip for item in parsed.events]
        trips += [item.trip for item in parsed.seen]
        trips += [item.trip for item in parsed.mutations]
        days = allowed_days(record, {trip.operating_day for trip in trips})
        events.extend(
            (event, "SIRI" if siri else "KV6")
            for event in parsed.events
            if event.trip.operating_day in days
        )
        mutations.extend(
            (item, "SIRI" if siri else "KV17")
            for item in parsed.mutations
            if item.trip.operating_day in days
        )
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
