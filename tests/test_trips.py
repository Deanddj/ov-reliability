from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ov_reliability.kv6.parse import Mutation, MutationType, StopEvent, StopEventType, TripKey
from ov_reliability.stats.gaps import Gap
from ov_reliability.stats.trips import Observations, Trip, build_trips, delay_of
from ov_reliability.timetables.plan import PlannedStop, PlannedTrip

DAY = date(2026, 10, 8)
T0 = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
MISSING_AFTER = timedelta(minutes=15)
NOW = T0 + timedelta(minutes=120)
EARLY = T0 - timedelta(minutes=60)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def key(journey: int = 1, planning_number: str = "3", reinforcement: int = 0) -> TripKey:
    return TripKey(DAY, "HTM", planning_number, journey, reinforcement)


def planned_trip(
    journey: int = 1, start: float = 0, stops: Sequence[str] = ("A", "B", "C")
) -> PlannedTrip:
    # Two minutes between stops, half a minute standing at each middle stop.
    return PlannedTrip(
        data_owner="HTM",
        planning_number="3",
        public_code="3",
        mode="tram",
        operating_day=DAY,
        journey_number=journey,
        direction="outbound",
        destination="Zoetermeer",
        stops=tuple(
            PlannedStop(
                i + 1,
                code,
                f"Stop {code}",
                at(start + 2 * i),
                at(start + 2 * i + (0.5 if 0 < i < len(stops) - 1 else 0)),
            )
            for i, code in enumerate(stops)
        ),
    )


def event(
    stop: str,
    kind: StopEventType,
    minutes: float,
    punctuality: int = 30,
    journey: int = 1,
) -> StopEvent:
    return StopEvent(key(journey), stop, 0, kind, at(minutes), punctuality, at(minutes))


def mutation(
    kind: MutationType, minutes: float, stop: str | None = None, journey: int = 1
) -> Mutation:
    passage = 0 if stop is not None else None
    return Mutation(key(journey), kind, stop, passage, at(minutes), at(minutes))


def observed(
    *events: StopEvent, mutations: Sequence[Mutation] = (), seen: bool | None = None
) -> Observations:
    span = None
    if seen or (seen is None and events):
        times = [e.event_at for e in events] or [T0]
        span = (min(times), max(times))
    return Observations(tuple(events), tuple(mutations), span)


def build(
    observations: dict[TripKey, Observations] | None = None,
    planned: Sequence[PlannedTrip] | None = None,
    gaps: Sequence[Gap] = (),
    data_until: datetime | None = NOW,
    now: datetime = NOW,
    start: datetime = EARLY,
    end: datetime = NOW,
    realtime: bool = True,
) -> list[Trip]:
    return build_trips(
        [planned_trip()] if planned is None else planned,
        observations or {},
        gaps,
        data_until,
        now,
        start,
        end,
        MISSING_AFTER,
        realtime,
    )


def only(trips: list[Trip]) -> Trip:
    assert len(trips) == 1
    return trips[0]


@pytest.mark.parametrize(
    ("punctuality", "delay"),
    [(0, 0), (42, 42), (-35, -35), (3599, 3599), (3600, None), (-3600, None), (-9999, None)],
)
def test_delay_of(punctuality: int, delay: int | None) -> None:
    assert delay_of(punctuality) == delay


def test_ran_with_departures() -> None:
    trip = only(
        build(
            {
                key(): observed(
                    event("A", "DEPARTURE", 0.7, 42),
                    event("B", "ARRIVAL", 2.5, 30),
                    event("B", "DEPARTURE", 3.0, 35),
                    event("C", "ARRIVAL", 4.8, 48),
                )
            }
        )
    )
    assert (trip.status, trip.planned, trip.planned_start) == ("ran", True, at(0))
    assert [(d.stop_code, d.sequence, d.planned) for d in trip.departures] == [
        ("A", 1, at(0)),
        ("B", 2, at(2.5)),
        ("C", 3, at(4)),
    ]
    assert [(d.actual, d.delay_seconds, d.event) for d in trip.departures] == [
        (at(0.7), 42, "DEPARTURE"),
        (at(3.0), 35, "DEPARTURE"),
        (at(4.8), 48, "ARRIVAL"),
    ]
    assert trip.departures[1].stop_name == "Stop B"


def test_last_departure_of_a_passage_and_first_arrival_at_the_last_stop() -> None:
    trip = only(
        build(
            {
                key(): observed(
                    event("A", "DEPARTURE", -3, -180),
                    event("A", "DEPARTURE", 0.5, 30),
                    event("A", "ARRIVAL", -4, -240),
                    event("C", "ARRIVAL", 4.5, 30),
                    event("C", "ARRIVAL", 5, 60),
                    event("C", "DEPARTURE", 6, 120),
                )
            }
        )
    )
    a, b, c = trip.departures
    assert (a.actual, a.delay_seconds) == (at(0.5), 30)
    assert (b.actual, b.delay_seconds, b.event) == (None, None, None)
    assert (c.actual, c.delay_seconds, c.event) == (at(4.5), 30, "ARRIVAL")


def test_arrival_and_onstop_do_not_stand_in_for_a_departure() -> None:
    trip = only(build({key(): observed(event("B", "ARRIVAL", 2, 0), seen=True)}))
    assert trip.status == "ran"
    assert all(d.delay_seconds is None for d in trip.departures)


def test_clamped_punctuality_is_no_delay() -> None:
    trip = only(build({key(): observed(event("A", "DEPARTURE", 60, 3600))}))
    assert (trip.departures[0].actual, trip.departures[0].delay_seconds) == (at(60), None)


def test_seen_only_by_onstop_is_ran() -> None:
    assert only(build({key(): observed(seen=True)})).status == "ran"


@pytest.mark.parametrize(
    ("mutations", "status"),
    [
        (["CANCEL"], "cancelled"),
        (["CANCEL", "RECOVER"], "missing"),
        (["RECOVER", "CANCEL"], "cancelled"),
        (["NOTMONITORED"], "unknown"),
        (["CANCEL", "NOTMONITORED"], "unknown"),
    ],
)
def test_the_latest_journey_mutation_decides(mutations: list[MutationType], status: str) -> None:
    found = [mutation(kind, -60 + i) for i, kind in enumerate(mutations)]
    assert only(build({key(): observed(mutations=found)})).status == status


def test_a_stop_event_wins_over_a_cancellation() -> None:
    found = observed(event("A", "DEPARTURE", 0), mutations=[mutation("CANCEL", -10)])
    assert only(build({key(): found})).status == "ran"


def test_every_departure_of_a_cancelled_trip_is_cancelled() -> None:
    trip = only(build({key(): observed(mutations=[mutation("CANCEL", -10)])}))
    assert trip.departures
    assert all(d.cancelled and d.delay_seconds is None for d in trip.departures)


def test_a_cancelled_trip_in_the_future_is_cancelled() -> None:
    found = observed(mutations=[mutation("CANCEL", -10)])
    assert only(build({key(): found}, now=at(-30), end=at(60))).status == "cancelled"


def test_missing() -> None:
    assert only(build()).status == "missing"


def test_not_yet_due() -> None:
    assert only(build(now=at(14))).status == "not_yet_due"
    assert only(build(now=at(15))).status == "missing"


def test_unknown_without_realtime() -> None:
    assert only(build(realtime=False)).status == "unknown"
    assert only(build(realtime=False, now=at(5))).status == "not_yet_due"


@pytest.mark.parametrize(
    ("data_until", "status"), [(None, "unknown"), (at(14), "unknown"), (at(15), "missing")]
)
def test_data_until_must_reach_the_due_time(data_until: datetime | None, status: str) -> None:
    assert only(build(data_until=data_until)).status == status


@pytest.mark.parametrize(
    ("gap_start", "gap_end", "status"),
    [
        (-30, 0, "missing"),
        (-30, 0.1, "unknown"),
        (18, 19, "unknown"),
        (19, 30, "missing"),
        (30, 40, "missing"),
    ],
)
def test_a_gap_in_the_trip_window_makes_it_unknown(
    gap_start: float, gap_end: float, status: str
) -> None:
    # The window runs from the planned start (0) to the last stop (4) plus 15 minutes.
    gap = Gap("collector", None, at(gap_start), at(gap_end), "disconnected")
    assert only(build(gaps=[gap])).status == status


def test_the_window_is_cut_at_now() -> None:
    gap = Gap("collector", None, at(16), at(17), "disconnected")
    assert only(build(gaps=[gap], now=at(15.5), data_until=at(15.5))).status == "missing"


def test_skipped_stop() -> None:
    found = observed(
        event("A", "DEPARTURE", 0.5),
        mutations=[mutation("SHORTEN", -5, "B"), mutation("SHORTEN", -5, "C")],
    )
    trip = only(build({key(): found}))
    assert [d.cancelled for d in trip.departures] == [False, True, True]
    assert trip.departures[1].delay_seconds is None


def test_a_stop_event_wins_over_a_skipped_stop() -> None:
    found = observed(event("B", "DEPARTURE", 3), mutations=[mutation("SHORTEN", -5, "B")])
    trip = only(build({key(): found}))
    assert [d.cancelled for d in trip.departures] == [False, False, False]
    assert trip.departures[1].delay_seconds == 30


def test_recover_brings_skipped_stops_back() -> None:
    found = observed(
        event("A", "DEPARTURE", 0.5),
        mutations=[
            mutation("SHORTEN", -5, "B"),
            mutation("RECOVER", -4),
            mutation("SHORTEN", -3, "C"),
        ],
    )
    trip = only(build({key(): found}))
    assert [d.cancelled for d in trip.departures] == [False, False, True]


def test_skipped_stops_of_a_trip_that_did_not_run() -> None:
    found = observed(mutations=[mutation("SHORTEN", -5, "C")])
    trip = only(build({key(): found}))
    assert trip.status == "missing"
    assert [d.cancelled for d in trip.departures] == [False, False, True]


def test_departures_are_cut_to_the_range_and_the_status_uses_the_whole_trip() -> None:
    found = observed(event("A", "DEPARTURE", 0.5), event("C", "ARRIVAL", 4))
    trip = only(build({key(): found}, start=at(1), end=at(3)))
    assert trip.status == "ran"
    assert [d.stop_code for d in trip.departures] == ["B"]


def test_trips_without_a_departure_in_the_range_are_left_out() -> None:
    assert build(start=at(4.5), end=at(60)) == []
    assert build(start=at(-30), end=at(0)) == []
    assert len(build(start=at(4), end=at(60))) == 1


def test_a_stop_outside_the_pattern() -> None:
    found = observed(
        event("A", "DEPARTURE", 0.5),
        event("X", "ARRIVAL", 1),
        event("X", "DEPARTURE", 1.5, 90),
        event("Y", "ARRIVAL", 2),
    )
    trip = only(build({key(): found}))
    extra = trip.departures[3:]
    assert [(d.stop_code, d.sequence, d.planned, d.actual, d.delay_seconds) for d in extra] == [
        ("X", None, None, at(1.5), 90)
    ]


def test_unplanned_trip() -> None:
    found = observed(
        event("B", "DEPARTURE", 30.5, 10, journey=9),
        event("A", "DEPARTURE", 28, 20, journey=9),
        event("C", "ARRIVAL", 32, 30, journey=9),
    )
    trips = build({key(9): found})
    assert [trip.journey_number for trip in trips] == [1, 9]
    trip = trips[1]
    assert (trip.planned, trip.status, trip.planned_start) == (False, "ran", None)
    assert (trip.direction, trip.destination) == (None, None)
    # No last stop is known, so only departures count; names come from the planned trips.
    assert [(d.stop_code, d.stop_name, d.delay_seconds) for d in trip.departures] == [
        ("A", "Stop A", 20),
        ("B", "Stop B", 10),
    ]


def test_unplanned_trip_seen_in_the_range_without_departures() -> None:
    found = Observations(seen=(at(10), at(20)))
    trips = build({key(9): found}, planned=[], start=at(15), end=at(30))
    assert [(t.journey_number, t.departures) for t in trips] == [(9, ())]
    assert build({key(9): found}, planned=[], start=at(21), end=at(30)) == []


def test_mutations_alone_are_no_unplanned_trip() -> None:
    found = observed(mutations=[mutation("CANCEL", 0, journey=9)])
    assert build({key(9): found}, planned=[]) == []


def test_reinforcement_trips_are_refused() -> None:
    found = Observations(seen=(at(0), at(1)))
    with pytest.raises(ValueError, match="reinforcement"):
        build({key(1, reinforcement=4): found})


# Properties over generated plans, observations, gaps and clocks.

STOPS = ("A", "B", "C", "D")
KINDS: tuple[StopEventType, ...] = ("ARRIVAL", "DEPARTURE")
MUTATIONS: tuple[MutationType, ...] = ("CANCEL", "RECOVER", "NOTMONITORED", "SHORTEN")
minutes = st.integers(-60, 240)


@st.composite
def scenarios(
    draw: st.DrawFn,
) -> tuple[list[PlannedTrip], dict[TripKey, Observations], list[Gap], datetime | None, datetime]:
    plans = [
        planned_trip(journey, draw(st.integers(0, 180)), draw(st.permutations(STOPS))[:3])
        for journey in range(1, draw(st.integers(0, 4)) + 1)
    ]
    observations = {}
    for journey in range(1, 7):
        events = [
            event(
                draw(st.sampled_from(STOPS)),
                draw(st.sampled_from(KINDS)),
                m,
                draw(st.sampled_from([0, 60, -30, 3600, -9999])),
                journey,
            )
            for m in draw(st.lists(minutes, max_size=4))
        ]
        found = []
        for m in draw(st.lists(minutes, max_size=3)):
            kind = draw(st.sampled_from(MUTATIONS))
            stop = draw(st.sampled_from(STOPS)) if kind == "SHORTEN" else None
            found.append(mutation(kind, m, stop, journey))
        seen = draw(st.booleans()) if not events else True
        observations[key(journey)] = observed(*events, mutations=found, seen=seen)
    gaps = [
        Gap("collector", None, at(s), at(s + length), "disconnected")
        for s, length in draw(st.lists(st.tuples(minutes, st.integers(1, 60)), max_size=2))
    ]
    now = at(draw(minutes))
    data_until = draw(st.none() | st.builds(at, minutes).filter(lambda d: d <= now))
    return plans, observations, gaps, data_until, now


def _plan_of(trip: Trip, plans: list[PlannedTrip]) -> PlannedTrip:
    return next(p for p in plans if p.journey_number == trip.journey_number)


@given(scenarios(), minutes, st.integers(1, 300), st.booleans())
def test_statuses_follow_the_contract(
    scenario: tuple[
        list[PlannedTrip], dict[TripKey, Observations], list[Gap], datetime | None, datetime
    ],
    start_minute: int,
    length: int,
    realtime: bool,
) -> None:
    plans, observations, gaps, data_until, now = scenario
    if not realtime:
        observations = {}
    start, end = at(start_minute), at(start_minute + length)
    trips = build(observations, plans, gaps, data_until, now, start, end, realtime)
    whole = {
        t.journey_number: t.status
        for t in build(observations, plans, gaps, data_until, now, at(-600), at(600), realtime)
    }
    for trip in trips:
        seen = observations.get(key(trip.journey_number), Observations())
        observed_trip = seen.seen is not None or bool(seen.events)
        # The status is decided on the whole trip, whatever the range.
        assert trip.status == whole[trip.journey_number]
        assert (trip.status == "ran") == observed_trip
        if not trip.planned:
            assert trip.planned_start is None and trip.status == "ran"
            continue
        plan = _plan_of(trip, plans)
        due = plan.start + MISSING_AFTER
        if trip.status == "missing":
            window_end = min(plan.stops[-1].arrival + MISSING_AFTER, now)
            assert realtime and now >= due
            assert data_until is not None and data_until >= due
            assert not any(g.start < window_end and g.end > plan.start for g in gaps)
        if trip.status == "not_yet_due":
            assert now < due
        if trip.status == "cancelled":
            assert all(d.cancelled for d in trip.departures)
        assert any(d.planned is not None for d in trip.departures)
        for departure in trip.departures:
            if departure.planned is not None:
                assert start <= departure.planned < end
            if departure.cancelled:
                assert departure.delay_seconds is None and departure.actual is None
            if departure.delay_seconds is not None:
                assert departure.event is not None and departure.actual is not None
    planned_in_range = {
        p.journey_number
        for p in plans
        if any(
            start <= (s.arrival if i == len(p.stops) - 1 else s.departure) < end
            for i, s in enumerate(p.stops)
        )
    }
    assert {t.journey_number for t in trips if t.planned} == planned_in_range
