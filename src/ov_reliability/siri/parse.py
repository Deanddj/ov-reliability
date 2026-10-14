"""Stop events and cancellations from SIRI EstimatedTimetable messages (decisions 067, 068, 087)."""

import xml.etree.ElementTree as ET
from collections.abc import Collection
from datetime import UTC, datetime

from ov_reliability.kv6.parse import (
    LOCAL,
    Mutation,
    MutationType,
    Parsed,
    StopEvent,
    StopEventType,
    TripKey,
    TripSeen,
    check_on_operating_day,
    event_time,
)
from ov_reliability.kv6.payload import BadMessageError

S = "{http://www.siri.org.uk/siri}"
_TIMES: tuple[tuple[StopEventType, str], ...] = (("ARRIVAL", "Arrival"), ("DEPARTURE", "Departure"))


def parse_estimated_timetable(
    root: ET.Element, received_at: datetime, owners: Collection[str]
) -> Parsed:
    events: list[StopEvent] = []
    seen: list[TripSeen] = []
    mutations: list[Mutation] = []
    bad: list[str] = []
    for journey in root.iter(f"{S}EstimatedVehicleJourney"):
        try:
            trip = _trip(journey)
            if trip.data_owner not in owners:
                continue
            found = _journey(trip, journey, received_at)
        except BadMessageError as exc:
            bad.append(f"SIRI: {exc}")
            continue
        events.extend(found.events)
        seen.extend(found.seen)
        mutations.extend(found.mutations)
    return Parsed(tuple(events), tuple(seen), tuple(mutations), tuple(bad))


def _journey(trip: TripKey, journey: ET.Element, received_at: datetime) -> Parsed:
    recorded = journey.findall(f"{S}RecordedCalls/{S}RecordedCall")
    calls = recorded + journey.findall(f"{S}EstimatedCalls/{S}EstimatedCall")
    stops = [_stop_code(call) for call in calls]
    # No planned metro trip visits a stop twice, so every call is passage 0.
    if len(set(stops)) != len(stops):
        raise BadMessageError(f"journey {trip.journey_number} names a stop twice")

    events: list[StopEvent] = []
    for call, stop in zip(recorded, stops[: len(recorded)], strict=True):
        for kind, name in _TIMES:
            actual = call.findtext(f"{S}Actual{name}Time")
            if not actual:
                continue
            planned = call.findtext(f"{S}Aimed{name}Time")
            if not planned:
                raise BadMessageError(f"Actual{name}Time without Aimed{name}Time at stop {stop}")
            at = event_time(actual, received_at)
            check_on_operating_day(trip, at)
            delay = round((at - _clock(planned)).total_seconds())
            events.append(StopEvent(trip, stop, 0, kind, at, delay, received_at))
    seen = [TripSeen(trip, event.event_at) for event in events]

    cancelled = journey.findtext(f"{S}Cancellation") == "true"
    skipped = [
        stop
        for call, stop in zip(calls, stops, strict=True)
        if call.findtext(f"{S}Cancellation") == "true"
    ]
    mutations: list[Mutation] = []
    if cancelled or skipped:
        aimed = [
            _clock(text)
            for call in calls
            for _, name in _TIMES
            if (text := call.findtext(f"{S}Aimed{name}Time"))
        ]
        if not aimed:
            raise BadMessageError(f"journey {trip.journey_number} is cancelled without times")
        # GVB resends every journey as cancelled after it ended; only a cancellation sent
        # before the trip's last stop is one (decision 068).
        if received_at < max(aimed):
            at = event_time(_required(journey, "RecordedAtTime"), received_at)
            if cancelled:
                mutations.append(_mutation(trip, "CANCEL", None, at, received_at))
            mutations += [_mutation(trip, "SHORTEN", stop, at, received_at) for stop in skipped]
    return Parsed(tuple(events), tuple(seen), tuple(mutations))


def _mutation(
    trip: TripKey, kind: MutationType, stop: str | None, at: datetime, received_at: datetime
) -> Mutation:
    return Mutation(trip, kind, stop, None if stop is None else 0, at, received_at)


def _trip(journey: ET.Element) -> TripKey:
    owner, _, planning_number = _parts(_required(journey, "LineRef"), "LineRef")
    day_text = _required(journey, f"FramedVehicleJourneyRef/{S}DataFrameRef")
    try:
        day = datetime.strptime(day_text, "%Y%m%d").date()
    except ValueError:
        raise BadMessageError(f"bad DataFrameRef {day_text!r}") from None
    reference = _required(journey, f"FramedVehicleJourneyRef/{S}DatedVehicleJourneyRef")
    number = _parts(reference, "DatedVehicleJourneyRef")[2]
    try:
        journey_number = int(number)
    except ValueError:
        raise BadMessageError(f"journey number is not an integer: {reference!r}") from None
    return TripKey(day, owner, planning_number, journey_number, 0)


def _stop_code(call: ET.Element) -> str:
    return _parts(_required(call, "StopPointRef"), "StopPointRef")[2]


def _parts(reference: str, name: str) -> tuple[str, str, str]:
    # GVB:Line:50, GVB:VehicleJourney:79567, GVB:StopPoint:03205
    parts = reference.split(":")
    if len(parts) != 3 or not all(parts):
        raise BadMessageError(f"bad {name} {reference!r}")
    return parts[0], parts[1], parts[2]


def _clock(text: str) -> datetime:
    """A planned time, which unlike an event may lie in the future."""
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        raise BadMessageError(f"bad time {text!r}") from None
    if at.tzinfo is None:
        at = at.replace(tzinfo=LOCAL)
    return at.astimezone(UTC)


def _required(element: ET.Element, path: str) -> str:
    value = element.findtext(f"{S}{path}")
    if not value:
        raise BadMessageError(f"missing {path.rpartition('}')[2]}")
    return value
