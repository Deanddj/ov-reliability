"""Counts and delay percentiles per direction, computed from the trip records (decision 048)."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ov_reliability.stats.trips import Trip


@dataclass(frozen=True)
class TripCounts:
    total: int
    ran: int
    cancelled: int
    missing: int
    unknown: int
    not_yet_due: int
    unplanned: int


@dataclass(frozen=True)
class DepartureCounts:
    total: int
    known_delay: int
    unknown_delay: int
    cancelled: int


@dataclass(frozen=True)
class Percentiles:
    p10: int | None
    p50: int | None
    p90: int | None


@dataclass(frozen=True)
class DirectionSummary:
    direction: str | None
    trips: TripCounts
    departures: DepartureCounts
    delay_seconds: Percentiles


def summarise(trips: Iterable[Trip]) -> list[DirectionSummary]:
    """One block per direction, trips without a direction in their own block, last."""
    by_direction: dict[str | None, list[Trip]] = {}
    for trip in trips:
        by_direction.setdefault(trip.direction, []).append(trip)
    order = sorted(by_direction, key=lambda direction: (direction is None, direction or ""))
    return [_summarise(direction, by_direction[direction]) for direction in order]


def nearest_rank(values: Sequence[int], percent: int) -> int | None:
    """The nearest-rank percentile: always one of the values (decision 082)."""
    if not values:
        return None
    # ceil(percent / 100 * n) in integers, so float rounding can't move the rank.
    rank = -(-percent * len(values) // 100)
    return sorted(values)[rank - 1]


def _summarise(direction: str | None, trips: list[Trip]) -> DirectionSummary:
    planned = [trip for trip in trips if trip.planned]
    statuses = [trip.status for trip in planned]
    departures = [departure for trip in trips for departure in trip.departures]
    delays = [d.delay_seconds for d in departures if d.delay_seconds is not None]
    return DirectionSummary(
        direction=direction,
        trips=TripCounts(
            total=len(trips),
            ran=statuses.count("ran"),
            cancelled=statuses.count("cancelled"),
            missing=statuses.count("missing"),
            unknown=statuses.count("unknown"),
            not_yet_due=statuses.count("not_yet_due"),
            unplanned=len(trips) - len(planned),
        ),
        departures=DepartureCounts(
            total=len(departures),
            known_delay=len(delays),
            unknown_delay=len(departures) - len(delays),
            cancelled=sum(departure.cancelled for departure in departures),
        ),
        delay_seconds=Percentiles(
            nearest_rank(delays, 10), nearest_rank(delays, 50), nearest_rank(delays, 90)
        ),
    )
