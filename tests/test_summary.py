from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ov_reliability.kv6.parse import StopEventType
from ov_reliability.stats.summary import (
    DepartureCounts,
    Percentiles,
    TripCounts,
    nearest_rank,
    summarise,
)
from ov_reliability.stats.trips import Departure, Status, Trip

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
STATUSES: tuple[Status, ...] = ("ran", "cancelled", "missing", "unknown", "not_yet_due")


def departure(delay: int | None = None, cancelled: bool = False) -> Departure:
    event: StopEventType | None = "DEPARTURE" if delay is not None else None
    actual = T0 if delay is not None else None
    return Departure("A", "Alpha", 1, T0, actual, delay, event, cancelled)


def trip(
    direction: str | None,
    status: Status = "ran",
    planned: bool = True,
    departures: tuple[Departure, ...] = (),
    journey: int = 1,
) -> Trip:
    return Trip(
        date(2026, 10, 8),
        "3",
        journey,
        planned,
        direction,
        None,
        T0 if planned else None,
        status,
        departures,
    )


@pytest.mark.parametrize(
    ("values", "p10", "p50", "p90"),
    [
        ([], None, None, None),
        ([42], 42, 42, 42),
        ([5, 1], 1, 1, 5),
        ([3, 1, 2], 1, 2, 3),
        (list(range(1, 11)), 1, 5, 9),
        (list(range(1, 12)), 2, 6, 10),
    ],
)
def test_nearest_rank(values: list[int], p10: int | None, p50: int | None, p90: int | None) -> None:
    assert [nearest_rank(values, p) for p in (10, 50, 90)] == [p10, p50, p90]


def test_summary_per_direction() -> None:
    trips = [
        trip("outbound", departures=(departure(60), departure(-30), departure(None))),
        trip("outbound", "cancelled", departures=(departure(cancelled=True),)),
        trip("outbound", planned=False, departures=(departure(120),)),
        trip(None, "unknown", departures=(departure(),)),
        trip("inbound", "missing"),
    ]
    inbound, outbound, none = summarise(trips)
    assert (inbound.direction, outbound.direction, none.direction) == ("inbound", "outbound", None)
    assert outbound.trips == TripCounts(3, 1, 1, 0, 0, 0, 1)
    assert outbound.departures == DepartureCounts(5, 3, 2, 1)
    assert outbound.delay_seconds == Percentiles(-30, 60, 120)
    assert none.trips == TripCounts(1, 0, 0, 0, 1, 0, 0)
    assert none.delay_seconds == Percentiles(None, None, None)
    assert inbound.departures == DepartureCounts(0, 0, 0, 0)


def test_no_trips_is_no_blocks() -> None:
    assert summarise([]) == []


delays = st.none() | st.integers(-600, 3000)


@st.composite
def trip_lists(draw: st.DrawFn) -> list[Trip]:
    found = []
    for journey in range(draw(st.integers(0, 8))):
        planned = draw(st.booleans())
        departures = tuple(
            departure(delay, cancelled=delay is None and draw(st.booleans()))
            for delay in draw(st.lists(delays, max_size=5))
        )
        found.append(
            trip(
                draw(st.sampled_from(["inbound", "outbound", None])),
                draw(st.sampled_from(STATUSES)) if planned else "ran",
                planned,
                departures,
                journey,
            )
        )
    return found


@given(trip_lists())
def test_summary_properties(trips: list[Trip]) -> None:
    blocks = summarise(trips)
    assert sum(block.trips.total for block in blocks) == len(trips)
    assert len({block.direction for block in blocks}) == len(blocks)
    for block in blocks:
        members = [t for t in trips if t.direction == block.direction]
        counts = block.trips
        assert counts.total == len(members)
        assert counts.total == (
            counts.ran
            + counts.cancelled
            + counts.missing
            + counts.unknown
            + counts.not_yet_due
            + counts.unplanned
        )
        d = block.departures
        assert d.known_delay + d.unknown_delay == d.total
        assert d.cancelled <= d.unknown_delay
        known = [x.delay_seconds for t in members for x in t.departures]
        values = [x for x in known if x is not None]
        p = block.delay_seconds
        if d.known_delay == 0:
            assert p == Percentiles(None, None, None)
        else:
            assert p.p10 is not None and p.p50 is not None and p.p90 is not None
            assert p.p10 <= p.p50 <= p.p90
            assert {p.p10, p.p50, p.p90} <= set(values)
