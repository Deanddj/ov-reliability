"""Which real-time trip is which planned trip (decision 096).

A journey number is normally enough. GVB's SIRI feed also carries journeys of a changed metro
service (works) that the weekly timetable doesn't have, numbered `<line>0001` and up, and some
of those numbers equal the number of an unrelated planned trip. So a number only counts when the
times agree, and trips left over are paired with planned trips by stop and time.
"""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from statistics import median_low

from ov_reliability.kv6.parse import StopEvent, TripKey
from ov_reliability.stats.trips import Observations, delay_of
from ov_reliability.timetables.plan import PlannedTrip

# A trip must agree with the plan this closely over its whole run (the middle event), whether
# it was matched by number or by time.
KEEP_WITHIN = timedelta(minutes=5)
# An event coincides with the plan at a stop when it is aimed this close to the planned time.
PAIR_WITHIN = timedelta(seconds=90)

type _Group = tuple[date, str, str]  # operating day, data owner, planning number


def align_observations(
    planned: Sequence[PlannedTrip], observations: Mapping[TripKey, Observations]
) -> tuple[dict[TripKey, Observations], dict[TripKey, Observations]]:
    """Split the observations into those that belong to a planned trip and those that don't.

    The first mapping is keyed by planned trip. The second keeps the observations' own keys.
    """
    plans = {_key(trip): trip for trip in planned}
    matched: dict[TripKey, Observations] = {}
    loose: dict[TripKey, Observations] = {}
    for found_key, found in observations.items():
        trip = plans.get(found_key)
        if trip is not None and found_key.reinforcement == 0 and _agrees(trip, found):
            matched[found_key] = found
        else:
            loose[found_key] = found

    free = _by_stop([trip for key, trip in plans.items() if key not in matched])
    unplaced: dict[TripKey, Observations] = {}
    for found_key, found in sorted(loose.items(), key=lambda item: _first_aimed(item[1]) or _EPOCH):
        trip = _closest(free, found_key, found)
        if trip is None:
            unplaced[found_key] = found
        else:
            matched[_key(trip)] = found
            _use(free, trip)
    return matched, unplaced


_EPOCH = datetime.min.replace(tzinfo=UTC)


def _key(trip: PlannedTrip) -> TripKey:
    return TripKey(
        trip.operating_day, trip.data_owner, trip.planning_number, trip.journey_number, 0
    )


def _agrees(trip: PlannedTrip, found: Observations) -> bool:
    gap = _gap(trip, found.events)
    # With no event whose time can be judged (a cancellation, or clamped punctualities) the
    # number decides.
    return gap is None or gap <= KEEP_WITHIN


def _gap(trip: PlannedTrip, events: Sequence[StopEvent]) -> timedelta | None:
    """How far the aimed times of the events lie from the plan, in the middle."""
    gaps = []
    for event in events:
        aimed = _aimed(event)
        if aimed is None:
            continue
        times = [
            stop.arrival if event.event == "ARRIVAL" else stop.departure
            for stop in trip.stops
            if stop.stop_code == event.stop_code
        ]
        if times:
            gaps.append(min(abs(aimed - time) for time in times))
    return median_low(gaps) if gaps else None


def _aimed(event: StopEvent) -> datetime | None:
    delay = delay_of(event.punctuality)
    return None if delay is None else event.event_at - timedelta(seconds=delay)


def _first_judged(found: Observations) -> StopEvent | None:
    judged = [event for event in found.events if _aimed(event) is not None]
    return min(judged, key=lambda event: event.event_at) if judged else None


def _first_aimed(found: Observations) -> datetime | None:
    first = _first_judged(found)
    return None if first is None else _aimed(first)


type _Free = dict[tuple[_Group, str, str], list[tuple[datetime, PlannedTrip]]]


def _by_stop(trips: Sequence[PlannedTrip]) -> _Free:
    """Planned arrival and departure times, per day, line, stop and kind of event."""
    index: _Free = defaultdict(list)
    for trip in trips:
        for stop in trip.stops:
            index[(_group(trip), stop.stop_code, "ARRIVAL")].append((stop.arrival, trip))
            index[(_group(trip), stop.stop_code, "DEPARTURE")].append((stop.departure, trip))
    for entries in index.values():
        entries.sort(key=lambda entry: entry[0])
    return index


def _group(trip: PlannedTrip) -> _Group:
    return trip.operating_day, trip.data_owner, trip.planning_number


def _closest(free: _Free, found_key: TripKey, found: Observations) -> PlannedTrip | None:
    """The free planned trip that the observed trip passes the same stops at the same times as.

    Every event votes for the planned trips that have its stop at its aimed time, give or take
    PAIR_WITHIN; two services of one line don't pass a stop within that time of each other. The
    trip with the most votes wins, as long as the whole run stays within KEEP_WITHIN. It may run
    slower than planned between the stops (a diversion), so only the coincidences count.
    """
    if found_key.reinforcement != 0:
        return None
    group = (found_key.operating_day, found_key.data_owner, found_key.planning_number)
    votes: dict[int, tuple[int, PlannedTrip]] = {}
    for event in found.events:
        aimed = _aimed(event)
        if aimed is None:
            continue
        entries = free.get((group, event.stop_code, event.event), [])
        times = [time for time, _ in entries]
        for time, trip in entries[bisect_left(times, aimed - PAIR_WITHIN) :]:
            if time > aimed + PAIR_WITHIN:
                break
            count = votes.get(id(trip), (0, trip))[0]
            votes[id(trip)] = (count + 1, trip)
    best: tuple[int, timedelta, PlannedTrip] | None = None
    for count, trip in votes.values():
        gap = _gap(trip, found.events)
        if gap is None or gap > KEEP_WITHIN:
            continue
        if best is None or (count, -gap) > (best[0], -best[1]):
            best = (count, gap, trip)
    return None if best is None else best[2]


def _use(free: _Free, trip: PlannedTrip) -> None:
    for stop in trip.stops:
        for kind in ("ARRIVAL", "DEPARTURE"):
            entries = free.get((_group(trip), stop.stop_code, kind))
            if entries is not None:
                entries[:] = [entry for entry in entries if entry[1] is not trip]
