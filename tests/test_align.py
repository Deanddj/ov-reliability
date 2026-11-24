from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from ov_reliability.kv6.parse import StopEvent, TripKey
from ov_reliability.stats.align import align_observations
from ov_reliability.stats.trips import Observations
from ov_reliability.timetables.plan import PlannedStop, PlannedTrip

DAY = date(2026, 11, 21)
T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def key(journey: int, planning_number: str = "53") -> TripKey:
    return TripKey(DAY, "GVB", planning_number, journey, 0)


def plan(
    journey: int, start: float, planning_number: str = "53", stops: Sequence[str] = ("A", "B", "C")
) -> PlannedTrip:
    return PlannedTrip(
        data_owner="GVB",
        planning_number=planning_number,
        public_code=planning_number,
        mode="metro",
        operating_day=DAY,
        journey_number=journey,
        direction="outbound",
        destination="Centraal",
        stops=tuple(
            PlannedStop(i + 1, code, f"Stop {code}", at(start + 2 * i), at(start + 2 * i))
            for i, code in enumerate(stops)
        ),
    )


def seen(
    journey: int,
    start: float,
    planning_number: str = "53",
    stops: Sequence[str] = ("A", "B", "C"),
    late: int = 20,
    punctuality: int | None = None,
) -> Observations:
    """A trip that was aimed to start at `start` minutes and ran `late` seconds late."""
    events = tuple(
        StopEvent(
            key(journey, planning_number),
            code,
            0,
            "DEPARTURE",
            at(start + 2 * i) + timedelta(seconds=late),
            late if punctuality is None else punctuality,
            at(start + 2 * i),
        )
        for i, code in enumerate(stops)
    )
    return Observations(events, (), (events[0].event_at, events[-1].event_at))


def test_a_number_match_with_the_same_times_is_kept() -> None:
    observations = {key(1): seen(1, 0)}
    matched, unplaced = align_observations([plan(1, 0)], observations)
    assert matched == observations
    assert unplaced == {}


def test_a_number_match_with_other_times_is_not_a_match() -> None:
    # The plan's journey 1 starts at 09:00; the real journey 1 is aimed at 18:19.
    observations = {key(1): seen(1, 559)}
    matched, unplaced = align_observations([plan(1, 0)], observations)
    assert matched == {}
    assert unplaced == observations


def test_a_real_trip_without_a_plan_pairs_with_the_planned_trip_of_its_time() -> None:
    observations = {key(530001): seen(530001, 30)}
    matched, unplaced = align_observations([plan(70220, 30)], observations)
    assert matched == {key(70220): observations[key(530001)]}
    assert unplaced == {}


def test_a_collision_and_the_trip_it_hides_both_find_their_places() -> None:
    # Real journey 1 is aimed at 30 minutes, where the plan has journey 2; the plan's own
    # journey 1 (at 0 minutes) was not seen.
    observations = {key(1): seen(1, 30)}
    matched, unplaced = align_observations([plan(1, 0), plan(2, 30)], observations)
    assert matched == {key(2): observations[key(1)]}
    assert unplaced == {}


def test_a_real_trip_is_paired_with_the_closest_planned_trip() -> None:
    observations = {key(9): seen(9, 30.4, late=0)}
    matched, _ = align_observations([plan(1, 30), plan(2, 31)], observations)
    assert list(matched) == [key(1)]


def slowed(journey: int, start: float, extra_minutes: Sequence[int]) -> Observations:
    """A trip that starts on time and is `extra_minutes` behind the plan at each stop."""
    stops = ("A", "B", "C", "D")[: len(extra_minutes)]
    events = tuple(
        StopEvent(
            key(journey),
            code,
            0,
            "DEPARTURE",
            at(start + 2 * i + extra),
            0,
            at(start + 2 * i),
        )
        for i, (code, extra) in enumerate(zip(stops, extra_minutes, strict=True))
    )
    return Observations(events, (), (events[0].event_at, events[-1].event_at))


def test_a_trip_that_starts_on_time_but_runs_slower_still_pairs() -> None:
    # A diversion has other running times: 0, 2, 3 and 4 minutes behind the plan.
    observations = {key(9): slowed(9, 30, [0, 2, 3, 4])}
    planned = plan(1, 30, stops=("A", "B", "C", "D"))
    matched, unplaced = align_observations([planned], observations)
    assert matched == {key(1): observations[key(9)]}
    assert unplaced == {}


def test_a_trip_that_starts_together_but_ends_far_off_is_not_paired() -> None:
    observations = {key(9): slowed(9, 30, [0, 10, 20])}
    matched, unplaced = align_observations([plan(1, 30)], observations)
    assert matched == {}
    assert unplaced == observations


def test_a_trip_pairs_by_the_stops_it_shares_when_its_first_stop_is_not_in_the_plan() -> None:
    observations = {key(9): seen(9, 28, stops=("X", "A", "B", "C"))}
    # The real trip calls at X at minute 28, then at A, B and C two minutes apart: minute 30
    # at A, like the plan.
    matched, unplaced = align_observations([plan(1, 30)], observations)
    assert matched == {key(1): observations[key(9)]}
    assert unplaced == {}


def test_an_arrival_is_compared_with_the_planned_arrival() -> None:
    planned = PlannedTrip(
        data_owner="GVB",
        planning_number="53",
        public_code="53",
        mode="metro",
        operating_day=DAY,
        journey_number=1,
        direction="outbound",
        destination="Centraal",
        stops=(
            PlannedStop(1, "A", "Stop A", at(29), at(30)),
            PlannedStop(2, "B", "Stop B", at(32), at(32)),
        ),
    )
    arrival = StopEvent(key(9), "A", 0, "ARRIVAL", at(29), 0, at(29))
    observations = {key(9): Observations((arrival,), (), (at(29), at(29)))}
    matched, _ = align_observations([planned], observations)
    assert list(matched) == [key(1)]


def test_pairing_is_one_to_one() -> None:
    observations = {key(8): seen(8, 30), key(9): seen(9, 30)}
    matched, unplaced = align_observations([plan(1, 30)], observations)
    assert len(matched) == 1
    assert len(unplaced) == 1


def test_a_planned_trip_that_was_seen_by_number_is_not_given_a_second_trip() -> None:
    observations = {key(1): seen(1, 30), key(9): seen(9, 30)}
    matched, unplaced = align_observations([plan(1, 30)], observations)
    assert matched == {key(1): observations[key(1)]}
    assert unplaced == {key(9): observations[key(9)]}


def test_no_pairing_when_the_times_are_ninety_seconds_apart() -> None:
    observations = {key(9): seen(9, 30, late=0)}
    matched, unplaced = align_observations([plan(1, 30 + 91 / 60)], observations)
    assert matched == {}
    assert unplaced == observations


def test_other_planning_numbers_are_never_paired() -> None:
    observations = {key(9, "54"): seen(9, 30, "54")}
    matched, unplaced = align_observations([plan(1, 30, "53")], observations)
    assert matched == {}
    assert unplaced == observations


def test_a_trip_that_shares_no_stop_with_the_plan_is_not_paired() -> None:
    observations = {key(9): seen(9, 30, stops=("X", "Y", "Z"))}
    matched, unplaced = align_observations([plan(1, 30)], observations)
    assert matched == {}
    assert unplaced == observations


def test_clamped_punctuality_cannot_be_judged_so_the_number_decides() -> None:
    observations = {key(1): seen(1, 559, punctuality=3600)}
    matched, unplaced = align_observations([plan(1, 0)], observations)
    assert matched == observations
    assert unplaced == {}


def test_a_cancellation_without_events_stays_with_its_number() -> None:
    cancelled = Observations((), (), None)
    matched, unplaced = align_observations([plan(1, 0)], {key(1): cancelled})
    assert matched == {key(1): cancelled}
    assert unplaced == {}


def test_reinforcement_trips_are_left_alone() -> None:
    reinforcement = TripKey(DAY, "GVB", "53", 1, 1)
    observations = {reinforcement: seen(1, 0)}
    matched, unplaced = align_observations([plan(1, 0)], observations)
    assert matched == {}
    assert unplaced == observations


@given(
    planned_starts=st.lists(st.integers(0, 600), unique=True, max_size=12),
    seen_starts=st.lists(st.integers(0, 600), unique=True, max_size=12),
    plan_numbers=st.permutations(range(1, 13)),
)
def test_every_observation_ends_up_in_exactly_one_place(
    planned_starts: list[int], seen_starts: list[int], plan_numbers: list[int]
) -> None:
    planned = [plan(plan_numbers[i], start) for i, start in enumerate(planned_starts)]
    observations = {key(500 + i): seen(500 + i, start) for i, start in enumerate(seen_starts)}
    matched, unplaced = align_observations(planned, observations)
    plan_keys = {key(trip.journey_number) for trip in planned}
    assert set(matched) <= plan_keys
    assert len(matched) + len(unplaced) == len(observations)
    placed = [*matched.values(), *unplaced.values()]
    assert sorted(map(id, placed)) == sorted(map(id, observations.values()))
