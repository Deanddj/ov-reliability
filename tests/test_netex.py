from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from netexfile import NAME, SMALL, write_netex

from ov_reliability.timetables.netex import (
    LOCAL,
    Journey,
    Line,
    Stop,
    StopProfile,
    TimetableError,
    TimetableHeader,
    read_header,
    read_timetable,
)

WEEK = frozenset(date(2026, 10, 5) + timedelta(days=i) for i in range(7))
SUNDAY = frozenset({date(2026, 10, 11)})


def journeys(path: Path) -> dict[int, Journey]:
    return {journey.journey_number: journey for journey in read_timetable(path).journeys}


def test_header(tmp_path: Path) -> None:
    assert read_header(write_netex(tmp_path)) == TimetableHeader(
        operator="htm",
        series="Rail",
        file_name=NAME,
        published_at=datetime(2026, 10, 1, 9, 0, tzinfo=LOCAL),
        valid_from=date(2026, 10, 5),
        valid_to=date(2026, 10, 11),
    )


def test_timetable_header_matches_read_header(tmp_path: Path) -> None:
    path = write_netex(tmp_path)
    assert read_timetable(path).header == read_header(path)


def test_lines_by_planning_number(tmp_path: Path) -> None:
    assert read_timetable(write_netex(tmp_path)).lines == {
        "3": Line("3", "tram", "Zoetermeer - Loosduinen"),
        "53": Line("3", "tram", "Zoetermeer - Den Haag Centraal"),
        "4": Line("4", "tram", "Lansingerland - Uithof"),
    }


def test_journeys(tmp_path: Path) -> None:
    timetable = read_timetable(write_netex(tmp_path))
    found = {j.journey_number: j for j in timetable.journeys}
    assert found.keys() == {3001, 3002, 3003, 3004, 53001, 4001}
    # Day bits before and after the file's validity are dropped.
    assert found[3001] == Journey("3", 3001, found[3001].profile, 5 * 3600 + 31 * 60, 0, WEEK)
    assert found[3003] == Journey("3", 3003, found[3001].profile, 10 * 60, 1, SUNDAY)
    assert found[3004] == Journey("3", 3004, found[3002].profile, 2 * 3600 + 30 * 60, 0, SUNDAY)
    assert found[53001].planning_number == "53"
    assert len(timetable.profiles) == 4


def test_profile_walks_timing_points_and_waits(tmp_path: Path) -> None:
    timetable = read_timetable(write_netex(tmp_path))
    found = {j.journey_number: j for j in timetable.journeys}
    # Alpha -> timing point (run 90.5 s, wait 20 s) -> Beta (run 45 s, wait 60 s) -> Gamma (120 s).
    assert timetable.profiles[found[3001].profile] == StopProfile(
        direction="outbound",
        destination="Zoetermeer Centrum",
        stops=(
            Stop(1, "1001", "Alpha", 0, 0),
            Stop(2, "1002", "Beta", 156, 216),
            Stop(3, "1003", "Gamma", 336, 336),
        ),
    )
    assert timetable.profiles[found[3002].profile] == StopProfile(
        direction="inbound",
        destination="Den Haag Loosduinen",
        stops=(
            Stop(1, "1003", "Gamma", 0, 0),
            Stop(2, "1002", "Beta", 180, 180),
            Stop(3, "1001", "Alpha", 300, 300),
        ),
    )


def test_absent_direction_and_destination_are_none(tmp_path: Path) -> None:
    timetable = read_timetable(write_netex(tmp_path))
    found = {j.journey_number: j for j in timetable.journeys}
    profile = timetable.profiles[found[4001].profile]
    assert (profile.direction, profile.destination) == (None, None)


def test_file_without_journeys(tmp_path: Path) -> None:
    start, end = SMALL.index("<vehicleJourneys>"), SMALL.index("</vehicleJourneys>")
    text = SMALL[: start + len("<vehicleJourneys>")] + SMALL[end:]
    timetable = read_timetable(write_netex(tmp_path, text))
    assert timetable.journeys == ()
    assert timetable.profiles == ()


def test_missing_to_date_is_open_ended(tmp_path: Path) -> None:
    text = SMALL.replace("<ToDate>2026-10-11T00:00:00</ToDate>", "", 1)
    path = write_netex(tmp_path, text)
    assert read_header(path).valid_to is None
    assert journeys(path)[3001].days == WEEK | {date(2026, 10, 12)}


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            'RouteRef ref="NL:T:Route:53"',
            'RouteRef ref="NL:T:Route:99"',
            "route NL:T:Route:99 does not exist",
        ),
        (
            'ref="NL:T:DestinationDisplay:2"',
            'ref="NL:T:DestinationDisplay:9"',
            "destination NL:T:DestinationDisplay:9 does not exist",
        ),
        (
            '<ScheduledStopPointRef ref="NL:T:ScheduledStopPoint:3" version="1"/>\n'
            "                </StopPointInJourneyPattern>",
            '<ScheduledStopPointRef ref="NL:T:ScheduledStopPoint:7" version="1"/>\n'
            "                </StopPointInJourneyPattern>",
            "stop NL:T:ScheduledStopPoint:7 does not exist",
        ),
        ("<RunTime>PT45S</RunTime>", "", "has no RunTime"),
        (
            '<TimingLinkRef ref="NL:T:TimingLink:T1-2"',
            '<TimingLinkRef ref="NL:T:TimingLink:T1-9"',
            "no run time for NL:T:TimingLink:T1-2",
        ),
        ('version="1" order="2"', 'version="1" order="5"', "order differs from document order"),
        ("<DepartureTime>23:50:00", "<DepartureTime>24:10:00", "bad DepartureTime"),
        ('"JourneyNumber">3001', '"JourneyNumber">', "is empty"),
        ('"LinePlanningNumber">53', '"LinePlanningNumber">', "is empty"),
        ("<PublicationTimestamp>2026-10-01T09:00:00</PublicationTimestamp>", "", "Timestamp"),
    ],
)
def test_content_errors(tmp_path: Path, old: str, new: str, reason: str) -> None:
    assert old in SMALL
    with pytest.raises(TimetableError, match=reason) as error:
        read_timetable(write_netex(tmp_path, SMALL.replace(old, new)))
    assert error.value.file == NAME


@pytest.mark.parametrize(
    ("name", "operator"),
    [
        ("timetable.xml.gz", "htm"),
        ("NeTEx_HTM.xml.gz", "htm"),
        (NAME, "ret"),
    ],
)
def test_bad_file_name(tmp_path: Path, name: str, operator: str) -> None:
    path = write_netex(tmp_path, name=name, operator=operator)
    with pytest.raises(TimetableError, match="not a NeTEx_<operator>_<series>"):
        read_header(path)
    with pytest.raises(TimetableError, match="not a NeTEx_<operator>_<series>"):
        read_timetable(path)


def test_damaged_file(tmp_path: Path) -> None:
    path = write_netex(tmp_path)
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(TimetableError, match="cannot read"):
        read_timetable(path)


def test_header_without_publication_timestamp(tmp_path: Path) -> None:
    text = SMALL.replace("<PublicationTimestamp>2026-10-01T09:00:00</PublicationTimestamp>", "")
    with pytest.raises(TimetableError, match="no PublicationTimestamp"):
        read_header(write_netex(tmp_path, text))


def test_pattern_without_stops(tmp_path: Path) -> None:
    start = SMALL.index('<ServiceJourneyPattern id="NL:T:ServiceJourneyPattern:4"')
    end = SMALL.index("</ServiceJourneyPattern>", start)
    pattern = (
        SMALL[start:end]
        .replace("StopPointInJourneyPattern", "TimingPointInJourneyPattern")
        .replace("ScheduledStopPointRef", "TimingPointRef")
    )
    with pytest.raises(TimetableError, match="ServiceJourneyPattern:4 has no stops"):
        read_timetable(write_netex(tmp_path, SMALL[:start] + pattern + SMALL[end:]))
