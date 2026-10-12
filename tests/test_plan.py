import logging
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from netexfile import NAME, SMALL, index_line, write_netex

from ov_reliability.timetables.netex import (
    LOCAL,
    Journey,
    Line,
    Stop,
    StopProfile,
    TimetableError,
    TimetableFile,
    TimetableHeader,
    read_timetable,
)
from ov_reliability.timetables.plan import (
    LineName,
    NoTimetableError,
    PlannedStop,
    choose_files,
    load_plan,
    planned_trips,
    to_utc,
)

TRAM_3 = LineName("htm", "tram", "3")
SUNDAY = date(2026, 10, 11)
DAYS = [date(2026, 10, 5) + timedelta(days=i) for i in range(7)]
LINES = {
    "3": Line("3", "tram", "three"),
    "53": Line("3", "tram", "fifty-three"),
    "4": Line("4", "tram", "four"),
}


def utc(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def header(
    series: str = "Rail",
    published: int = 1,
    valid: tuple[int, int | None] = (5, 11),
    name: str = NAME,
) -> TimetableHeader:
    return TimetableHeader(
        operator="htm",
        series=series,
        file_name=name,
        published_at=datetime(2026, 10, published, 9, 0, tzinfo=LOCAL),
        valid_from=date(2026, 10, valid[0]),
        valid_to=None if valid[1] is None else date(2026, 10, valid[1]),
    )


def test_trips_of_a_line_on_a_day(tmp_path: Path) -> None:
    trips = planned_trips([read_timetable(write_netex(tmp_path))], TRAM_3, SUNDAY)
    # 3003 has DepartureDayOffset 1: Monday 00:10 belongs to Sunday's operating day.
    assert [(t.planning_number, t.journey_number) for t in trips] == [
        ("3", 3004),
        ("3", 3001),
        ("53", 53001),
        ("3", 3002),
        ("3", 3003),
    ]
    assert {t.operating_day for t in trips} == {SUNDAY}
    assert {(t.data_owner, t.public_code, t.mode) for t in trips} == {("HTM", "3", "tram")}
    first = trips[1]
    assert (first.direction, first.destination) == ("outbound", "Zoetermeer Centrum")
    assert first.stops == (
        PlannedStop(1, "1001", "Alpha", utc(11, 3, 31), utc(11, 3, 31)),
        PlannedStop(2, "1002", "Beta", utc(11, 3, 33, 36), utc(11, 3, 34, 36)),
        PlannedStop(3, "1003", "Gamma", utc(11, 3, 36, 36), utc(11, 3, 36, 36)),
    )
    assert trips[-1].start == utc(11, 22, 10)


def test_other_line_and_empty_day(tmp_path: Path) -> None:
    timetable = read_timetable(write_netex(tmp_path))
    trips = planned_trips([timetable], LineName("htm", "tram", "4"), SUNDAY)
    assert [(t.journey_number, t.direction, t.destination) for t in trips] == [(4001, None, None)]
    assert planned_trips([timetable], TRAM_3, date(2026, 10, 12)) == []
    assert planned_trips([timetable], LineName("htm", "bus", "3"), SUNDAY) == []


def test_planning_number_in_two_files_is_an_error(tmp_path: Path) -> None:
    timetable = read_timetable(write_netex(tmp_path))
    other = replace(timetable, header=replace(timetable.header, file_name="other.xml.gz"))
    with pytest.raises(TimetableError, match="planning number 3 is also in"):
        planned_trips([timetable, other], TRAM_3, SUNDAY)


def test_corrected_starts_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    profile = StopProfile(None, None, (Stop(1, "1", "A", 0, 0),))
    clock_change = date(2026, 10, 25)
    journeys = tuple(
        Journey("3", number, 0, clock, 0, frozenset({clock_change}))
        for number, clock in [(1, 2 * 3600 + 30 * 60), (2, 3 * 3600)]
    )
    timetable = TimetableFile(header(valid=(5, 31)), LINES, (profile,), journeys)
    with caplog.at_level(logging.WARNING):
        trips = planned_trips([timetable], TRAM_3, clock_change)
    assert [t.start for t in trips] == [utc(25, 0, 30), utc(25, 2, 0)]
    assert "1 trips on 2026-10-25 start in a clock-change hour" in caplog.text


@st.composite
def timetables(draw: st.DrawFn) -> TimetableFile:
    profiles = []
    for _ in range(draw(st.integers(1, 3))):
        stops, elapsed = [], 0
        for sequence in range(1, draw(st.integers(1, 5)) + 1):
            wait = draw(st.integers(0, 300))
            stops.append(Stop(sequence, str(sequence), "stop", elapsed, elapsed + wait))
            elapsed += wait + draw(st.integers(0, 900))
        profiles.append(StopProfile("inbound", None, tuple(stops)))
    journeys = draw(
        st.lists(
            st.builds(
                Journey,
                planning_number=st.sampled_from(sorted(LINES)),
                journey_number=st.integers(1, 99_999),
                profile=st.integers(0, len(profiles) - 1),
                start_clock=st.integers(0, 86_399),
                start_day_offset=st.integers(0, 1),
                days=st.frozensets(st.sampled_from(DAYS)),
            ),
            max_size=20,
        )
    )
    return TimetableFile(header(), LINES, tuple(profiles), tuple(journeys))


@given(timetable=timetables(), day=st.sampled_from(DAYS))
def test_trip_properties(timetable: TimetableFile, day: date) -> None:
    trips = planned_trips([timetable], TRAM_3, day)
    for trip in trips:
        start, _ = to_utc(day, 0, 0)
        assert trip.start == trip.stops[0].arrival >= start
        for stop, following in zip(trip.stops, trip.stops[1:], strict=False):
            assert stop.arrival <= stop.departure <= following.arrival
        assert trip.stops[-1].arrival <= trip.stops[-1].departure
    expected = Counter(
        (j.planning_number, j.journey_number, j.start_clock, j.start_day_offset)
        for j in timetable.journeys
        if day in j.days and j.planning_number in {"3", "53"}
    )
    got = Counter((t.planning_number, t.journey_number, *_clock(t.start, day)) for t in trips)
    assert got == expected


def _clock(start: datetime, day: date) -> tuple[int, int]:
    local = start.astimezone(LOCAL)
    offset = (local.date() - day).days
    return local.hour * 3600 + local.minute * 60 + local.second, offset


@given(
    headers=st.lists(
        st.builds(
            header,
            series=st.sampled_from(["Bus", "Rail"]),
            published=st.integers(1, 9),
            valid=st.tuples(st.integers(1, 15), st.none() | st.integers(1, 20)),
        ),
        max_size=8,
    ),
    day=st.integers(1, 20).map(lambda d: date(2026, 10, d)),
)
def test_choose_files_properties(headers: list[TimetableHeader], day: date) -> None:
    chosen = choose_files(headers, "htm", day)
    assert len({h.series for h in chosen}) == len(chosen)
    for picked in chosen:
        assert picked.covers(day)
        assert not any(
            h.series == picked.series and h.covers(day) and h.published_at > picked.published_at
            for h in headers
        )
    assert {h.series for h in chosen} == {h.series for h in headers if h.covers(day)}
    assert choose_files(headers, "ret", day) == []


def test_choose_files_tie_takes_the_later() -> None:
    first, second = header(name="a.xml.gz"), header(name="b.xml.gz")
    assert choose_files([first, second], "htm", date(2026, 10, 6)) == [second]


@given(
    day=st.sampled_from([date(2026, 3, 29), date(2026, 10, 25)]),
    clock=st.integers(0, 5 * 3600),
)
def test_to_utc_around_clock_changes(day: date, clock: int) -> None:
    start, corrected = to_utc(day, 0, clock)
    local = start.astimezone(LOCAL)
    shown = local.hour * 3600 + local.minute * 60 + local.second
    in_changed_hour = 2 * 3600 <= clock < 3 * 3600
    assert corrected == in_changed_hour
    if day.month == 3 and in_changed_hour:
        assert shown == clock + 3600
    else:
        assert shown == clock
    assert local.date() == day
    if day.month == 10 and in_changed_hour:
        assert local.utcoffset() == timedelta(hours=2)


def test_to_utc_day_offset() -> None:
    assert to_utc(SUNDAY, 1, 10 * 60) == (utc(11, 22, 10), False)


def test_load_plan(tmp_path: Path) -> None:
    write_netex(tmp_path)
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{NAME}"))
    plan = load_plan(tmp_path, TRAM_3, SUNDAY)
    assert plan.files == (NAME,)
    assert [t.journey_number for t in plan.trips] == [3004, 3001, 53001, 3002, 3003]


def test_load_plan_without_a_file_for_the_day(tmp_path: Path) -> None:
    write_netex(tmp_path)
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{NAME}"))
    with pytest.raises(NoTimetableError, match="no timetable file of htm covers 2026-10-12"):
        load_plan(tmp_path, TRAM_3, date(2026, 10, 12))
    with pytest.raises(NoTimetableError):
        load_plan(tmp_path, LineName("ret", "tram", "3"), SUNDAY)


def test_load_plan_with_a_file_missing_on_disk(tmp_path: Path) -> None:
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{NAME}"))
    with pytest.raises(TimetableError, match=r"is in index\.jsonl but missing"):
        load_plan(tmp_path, TRAM_3, SUNDAY)


def test_load_plan_skips_a_half_written_last_line(tmp_path: Path) -> None:
    write_netex(tmp_path)
    later = "NeTEx_HTM_Rail_20261002_20261005_new.xml.gz"
    text = index_line(f"htm/{NAME}") + index_line(f"htm/{later}")[:40]
    (tmp_path / "index.jsonl").write_text(text)
    assert len(load_plan(tmp_path, TRAM_3, SUNDAY).trips) == 5


def test_load_plan_with_a_bad_index_line(tmp_path: Path) -> None:
    write_netex(tmp_path)
    (tmp_path / "index.jsonl").write_text("{}\n" + index_line(f"htm/{NAME}"))
    with pytest.raises(TimetableError, match="line 1 is invalid"):
        load_plan(tmp_path, TRAM_3, SUNDAY)


def test_load_plan_newer_file_of_a_series_wins(tmp_path: Path) -> None:
    newer = "NeTEx_HTM_Rail_20261003_20261005_new.xml.gz"
    write_netex(tmp_path)
    text = SMALL.replace("2026-10-01T09:00:00", "2026-10-03T09:00:00").replace(
        '"JourneyNumber">3001', '"JourneyNumber">3101'
    )
    write_netex(tmp_path, text, name=newer)
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{newer}") + index_line(f"htm/{NAME}"))
    plan = load_plan(tmp_path, TRAM_3, SUNDAY)
    assert plan.files == (newer,)
    assert 3101 in {t.journey_number for t in plan.trips}
    assert 3001 not in {t.journey_number for t in plan.trips}
