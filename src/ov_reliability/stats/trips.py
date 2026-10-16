"""Trips with their departures and statuses, from the plan and the real-time observations."""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal

from ov_reliability.kv6.parse import Mutation, StopEvent, StopEventType, TripKey
from ov_reliability.stats.gaps import Gap
from ov_reliability.timetables.plan import PlannedTrip

type Status = Literal["ran", "cancelled", "missing", "unknown", "not_yet_due"]

# Values that stand for "no delay known": a clamp at some operators and the XSD minimum.
_NO_DELAY = frozenset({3600, -3600, -9999})
_JOURNEY_MUTATIONS = frozenset({"CANCEL", "RECOVER", "NOTMONITORED"})


def delay_of(punctuality: int) -> int | None:
    """The delay a reported punctuality stands for (decision 067)."""
    return None if punctuality in _NO_DELAY else punctuality


@dataclass(frozen=True)
class Observations:
    """What the real-time sources reported for one trip."""

    events: tuple[StopEvent, ...] = ()
    mutations: tuple[Mutation, ...] = ()
    # First and last ARRIVAL, ONSTOP or DEPARTURE; None when the trip was never seen.
    seen: tuple[datetime, datetime] | None = None


@dataclass(frozen=True)
class Departure:
    stop_code: str
    stop_name: str | None
    # None for a stop that isn't in the planned pattern (a diversion, or an unplanned trip).
    sequence: int | None
    planned: datetime | None
    actual: datetime | None
    delay_seconds: int | None
    event: StopEventType | None
    cancelled: bool


@dataclass(frozen=True)
class Trip:
    operating_day: date
    planning_number: str
    journey_number: int
    planned: bool
    direction: str | None
    destination: str | None
    planned_start: datetime | None
    status: Status
    departures: tuple[Departure, ...]


def build_trips(
    planned: Sequence[PlannedTrip],
    observations: Mapping[TripKey, Observations],
    gaps: Sequence[Gap],
    data_until: datetime | None,
    now: datetime,
    start: datetime,
    end: datetime,
    missing_after: timedelta,
    realtime: bool,
) -> list[Trip]:
    """The trips with a departure in [start, end), with statuses decided on the whole trip.

    `gaps` are the gaps of the line's recorder and topics; `data_until` is how far that
    recorder's data is loaded. Without `realtime` no trip can be `missing`.
    """
    names = {stop.stop_code: stop.stop_name for trip in planned for stop in trip.stops}
    trips: list[tuple[datetime, Trip]] = []
    planned_keys: set[TripKey] = set()
    for plan in planned:
        key = TripKey(
            plan.operating_day, plan.data_owner, plan.planning_number, plan.journey_number, 0
        )
        planned_keys.add(key)
        seen = observations.get(key, Observations())
        departures = _in_range(_planned_departures(plan, seen, names), start, end)
        if not any(departure.sequence is not None for departure in departures):
            continue
        status = _status(plan, seen, gaps, data_until, now, missing_after, realtime)
        if status == "cancelled":
            # Every stop of a cancelled trip was called off, as seen from the platform.
            departures = tuple(replace(departure, cancelled=True) for departure in departures)
        trips.append(
            (
                plan.start,
                Trip(
                    plan.operating_day,
                    plan.planning_number,
                    plan.journey_number,
                    True,
                    plan.direction,
                    plan.destination,
                    plan.start,
                    status,
                    departures,
                ),
            )
        )
    for key, seen in observations.items():
        if key in planned_keys or (seen.seen is None and not seen.events):
            continue
        if key.reinforcement != 0:
            # How reinforcement trips appear is decided when a line that has them is served.
            raise ValueError(f"reinforcement trip {key} can't be shown yet")
        extra = _extra_departures(seen, set(), names)
        departures = _in_range(extra, start, end)
        overlaps = seen.seen is not None and seen.seen[0] < end and seen.seen[1] >= start
        if not departures and not overlaps:
            continue
        first = seen.seen[0] if seen.seen else min(e.event_at for e in seen.events)
        trips.append(
            (
                first,
                Trip(
                    key.operating_day,
                    key.planning_number,
                    key.journey_number,
                    False,
                    None,
                    None,
                    None,
                    "ran",
                    departures,
                ),
            )
        )
    trips.sort(key=lambda item: (item[0], item[1].planning_number, item[1].journey_number))
    return [trip for _, trip in trips]


def _status(
    plan: PlannedTrip,
    seen: Observations,
    gaps: Sequence[Gap],
    data_until: datetime | None,
    now: datetime,
    missing_after: timedelta,
    realtime: bool,
) -> Status:
    # What was observed wins over what was announced (decision 068).
    if seen.seen is not None or seen.events:
        return "ran"
    journey = [m for m in seen.mutations if m.mutation in _JOURNEY_MUTATIONS]
    latest = max(journey, key=_mutation_order).mutation if journey else None
    if latest == "CANCEL":
        return "cancelled"
    due = plan.start + missing_after
    if now < due:
        return "not_yet_due"
    if not realtime or latest == "NOTMONITORED" or data_until is None or data_until < due:
        return "unknown"
    window_end = min(_planned_time(plan, len(plan.stops) - 1) + missing_after, now)
    if any(gap.start < window_end and gap.end > plan.start for gap in gaps):
        return "unknown"
    return "missing"


def _planned_departures(
    plan: PlannedTrip, seen: Observations, names: Mapping[str, str]
) -> list[Departure]:
    events = _by_passage(seen.events)
    skipped = _skipped(seen.mutations)
    visits: Counter[str] = Counter()
    passages: set[tuple[str, int]] = set()
    departures = []
    last = len(plan.stops) - 1
    for index, stop in enumerate(plan.stops):
        passage = (stop.stop_code, visits[stop.stop_code])
        visits[stop.stop_code] += 1
        passages.add(passage)
        found = events.get(passage, [])
        # At the last stop the first ARRIVAL, elsewhere the last DEPARTURE (decision 067).
        event = _first(found, "ARRIVAL") if index == last else _last(found, "DEPARTURE")
        departures.append(
            _departure(
                stop.stop_code,
                stop.stop_name,
                stop.sequence,
                _planned_time(plan, index),
                event,
                cancelled=passage in skipped and not found,
            )
        )
    return departures + _extra_departures(seen, passages, names)


def _extra_departures(
    seen: Observations, planned: set[tuple[str, int]], names: Mapping[str, str]
) -> list[Departure]:
    """Departures at stops outside the planned pattern, in the order they happened."""
    extra = []
    for passage, found in _by_passage(seen.events).items():
        event = _last(found, "DEPARTURE")
        if passage not in planned and event is not None:
            extra.append(event)
    return [
        _departure(event.stop_code, names.get(event.stop_code), None, None, event, False)
        for event in sorted(extra, key=_event_order)
    ]


def _departure(
    stop_code: str,
    stop_name: str | None,
    sequence: int | None,
    planned: datetime | None,
    event: StopEvent | None,
    cancelled: bool,
) -> Departure:
    return Departure(
        stop_code=stop_code,
        stop_name=stop_name,
        sequence=sequence,
        planned=planned,
        actual=event.event_at if event else None,
        delay_seconds=delay_of(event.punctuality) if event else None,
        event=event.event if event else None,
        cancelled=cancelled,
    )


def _in_range(
    departures: Iterable[Departure], start: datetime, end: datetime
) -> tuple[Departure, ...]:
    # A planned departure is selected by its planned time (decision 049), another by its actual.
    return tuple(
        departure
        for departure in departures
        if (moment := departure.planned or departure.actual) is not None and start <= moment < end
    )


def _planned_time(plan: PlannedTrip, index: int) -> datetime:
    # The last stop has no departure; its planned arrival stands in (decision 062).
    stop = plan.stops[index]
    return stop.arrival if index == len(plan.stops) - 1 else stop.departure


def _skipped(mutations: Iterable[Mutation]) -> set[tuple[str, int]]:
    """Passages whose latest SHORTEN has no later journey RECOVER (decision 068)."""
    recovered = max((m.mutated_at for m in mutations if m.mutation == "RECOVER"), default=None)
    return {
        (m.stop_code, m.passage)
        for m in mutations
        if m.mutation == "SHORTEN"
        and m.stop_code is not None
        and m.passage is not None
        and (recovered is None or m.mutated_at > recovered)
    }


def _by_passage(events: Iterable[StopEvent]) -> dict[tuple[str, int], list[StopEvent]]:
    found: dict[tuple[str, int], list[StopEvent]] = {}
    for event in events:
        found.setdefault((event.stop_code, event.passage), []).append(event)
    return found


def _first(events: Iterable[StopEvent], kind: StopEventType) -> StopEvent | None:
    return min((e for e in events if e.event == kind), key=_event_order, default=None)


def _last(events: Iterable[StopEvent], kind: StopEventType) -> StopEvent | None:
    return max((e for e in events if e.event == kind), key=_event_order, default=None)


def _event_order(event: StopEvent) -> tuple[datetime, datetime]:
    return event.event_at, event.received_at


def _mutation_order(mutation: Mutation) -> tuple[datetime, datetime, str]:
    return mutation.mutated_at, mutation.received_at, mutation.mutation
