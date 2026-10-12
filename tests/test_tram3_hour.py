from datetime import datetime, timedelta

import pytest
from tram3hour import observed, planned

from ov_reliability.kv6.parse import TripKey
from ov_reliability.stats.trips import Observations, Trip, build_trips
from ov_reliability.timetables.netex import LOCAL

# A disrupted evening: trams ran up to 75 minutes late and KV17 skipped many stops.
START = datetime(2026, 10, 8, 21, 0, tzinfo=LOCAL)
END = datetime(2026, 10, 8, 22, 0, tzinfo=LOCAL)
NEXT_DAY = datetime(2026, 10, 9, 12, 0, tzinfo=LOCAL)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 8, hour, minute, second, tzinfo=LOCAL)


def known_at(now: datetime) -> dict[TripKey, Observations]:
    """What had been received by `now`."""
    found = {}
    for key, seen in observed().items():
        span = (seen.seen[0], min(seen.seen[1], now)) if seen.seen and seen.seen[0] <= now else None
        found[key] = Observations(
            tuple(e for e in seen.events if e.received_at <= now),
            tuple(m for m in seen.mutations if m.received_at <= now),
            span,
        )
    return found


def hour(now: datetime, observations: dict[TripKey, Observations]) -> dict[int, Trip]:
    trips = build_trips(
        planned(), observations, [], now, now, START, END, timedelta(minutes=15), True
    )
    return {trip.journey_number: trip for trip in trips}


def test_every_trip_of_the_hour_ran() -> None:
    trips = hour(NEXT_DAY, observed())
    assert list(trips) == [
        30193, 30370, 30195, 30372, 30197, 30376, 30199, 30380, 30201,
        30384, 30203, 30388, 30205, 30392, 30207, 30396, 30209,
    ]  # fmt: skip
    assert {trip.status for trip in trips.values()} == {"ran"}


def test_a_trip_75_minutes_late() -> None:
    # Raw KV6: DEPARTURE at 1818 at 22:21:53.733 with punctuality 4522, planned 21:06:27.
    first = hour(NEXT_DAY, observed())[30384].departures[0]
    assert (first.stop_code, first.planned, first.delay_seconds) == ("1818", at(21, 6, 27), 4522)
    assert first.actual is not None
    assert first.actual.astimezone(LOCAL).replace(microsecond=0) == at(22, 21, 53)


def test_stops_skipped_after_a_recover() -> None:
    # Raw KV17 for 30201: RECOVER at 21:15:37, then SHORTEN of 8134 ... 8106 at 22:09:00.
    # The tram started again at 8104 (INIT only) and departed 8102 at 22:11:39, +3220.
    departures = {d.stop_code: d for d in hour(NEXT_DAY, observed())[30201].departures}
    skipped = ["8124", "8122", "8120", "8118", "8116", "8114", "8112", "8110", "8108", "8106"]
    assert [code for code, d in departures.items() if d.cancelled] == skipped
    assert (departures["8104"].cancelled, departures["8104"].delay_seconds) == (False, None)
    assert departures["8102"].delay_seconds == 3220


def test_a_cancel_after_the_trip_ran() -> None:
    # Raw for 30392: SHORTEN of every stop up to 8101 at 22:28, DEPARTURE at 8103 at 22:49
    # (+1430), CANCEL at 22:53. The departure wins; its stops in the hour were skipped.
    trip = hour(NEXT_DAY, observed())[30392]
    assert trip.status == "ran"
    assert len(trip.departures) == 17
    assert all(d.cancelled and d.delay_seconds is None for d in trip.departures)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            at(21, 30),
            {30201: "missing", 30384: "missing", 30203: "missing", 30392: "not_yet_due"},
        ),
        (
            at(22, 0),
            {30201: "missing", 30384: "missing", 30203: "ran", 30392: "missing"},
        ),
    ],
)
def test_late_trams_are_missing_until_they_report_in(
    now: datetime, expected: dict[int, str]
) -> None:
    trips = hour(now, known_at(now))
    assert {journey: trips[journey].status for journey in expected} == expected
