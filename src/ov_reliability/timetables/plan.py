"""The planned trips of a line on one day, from the archived NeTEx files."""

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from ov_reliability.config import load_timetables_config
from ov_reliability.runtime import EXIT_BAD_CONFIG, config_path
from ov_reliability.timetables.netex import (
    LOCAL,
    TimetableError,
    TimetableFile,
    TimetableHeader,
    read_header,
    read_timetable,
)
from ov_reliability.timetables.store import INDEX_NAME, IndexEntry

log = logging.getLogger(__name__)

EXIT_NO_PLAN = 1


@dataclass(frozen=True)
class LineName:
    operator: str  # the archive directory, e.g. "htm"
    mode: str
    public_code: str


@dataclass(frozen=True)
class PlannedStop:
    sequence: int
    stop_code: str
    stop_name: str
    arrival: datetime  # UTC
    departure: datetime


@dataclass(frozen=True)
class PlannedTrip:
    data_owner: str
    planning_number: str
    public_code: str
    mode: str
    operating_day: date
    journey_number: int
    direction: str | None
    destination: str | None
    stops: tuple[PlannedStop, ...]

    @property
    def start(self) -> datetime:
        return self.stops[0].arrival


@dataclass(frozen=True)
class Plan:
    files: tuple[str, ...]  # the timetable files the trips come from
    trips: tuple[PlannedTrip, ...]


class NoTimetableError(Exception):
    def __init__(self, operator: str, day: date) -> None:
        super().__init__(f"no timetable file of {operator} covers {day}")
        self.operator = operator
        self.day = day


def choose_files(
    headers: Iterable[TimetableHeader], operator: str, day: date
) -> list[TimetableHeader]:
    newest: dict[str, TimetableHeader] = {}
    for header in headers:
        if header.operator != operator or not header.covers(day):
            continue
        current = newest.get(header.series)
        # On a tie the later one wins: headers come in index order, and a republished file
        # can keep its PublicationTimestamp.
        if current is None or header.published_at >= current.published_at:
            newest[header.series] = header
    return sorted(newest.values(), key=lambda header: header.series)


def planned_trips(files: Iterable[TimetableFile], line: LineName, day: date) -> list[PlannedTrip]:
    trips: list[PlannedTrip] = []
    found_in: dict[str, str] = {}
    for timetable in files:
        file_name = timetable.header.file_name
        numbers = {
            number
            for number, found in timetable.lines.items()
            if found.public_code == line.public_code and found.mode == line.mode
        }
        for number in sorted(numbers):
            if number in found_in:
                raise TimetableError(
                    file_name, f"planning number {number} is also in {found_in[number]}"
                )
            found_in[number] = file_name
        corrected = 0
        for journey in timetable.journeys:
            if journey.planning_number not in numbers or day not in journey.days:
                continue
            start, was_corrected = to_utc(day, journey.start_day_offset, journey.start_clock)
            corrected += was_corrected
            profile = timetable.profiles[journey.profile]
            line_info = timetable.lines[journey.planning_number]
            trips.append(
                PlannedTrip(
                    data_owner=line.operator.upper(),
                    planning_number=journey.planning_number,
                    public_code=line_info.public_code,
                    mode=line_info.mode,
                    operating_day=day,
                    journey_number=journey.journey_number,
                    direction=profile.direction,
                    destination=profile.destination,
                    stops=tuple(
                        PlannedStop(
                            stop.sequence,
                            stop.stop_code,
                            stop.stop_name,
                            start + timedelta(seconds=stop.arrival_s),
                            start + timedelta(seconds=stop.departure_s),
                        )
                        for stop in profile.stops
                    ),
                )
            )
        if corrected:
            log.warning(
                "%s: %d trips on %s start in a clock-change hour; taken as summer time or "
                "moved an hour forward",
                file_name,
                corrected,
                day,
            )
    trips.sort(key=lambda trip: (trip.start, trip.planning_number, trip.journey_number))
    return trips


def to_utc(day: date, start_day_offset: int, start_clock: int) -> tuple[datetime, bool]:
    wall = datetime.combine(day + timedelta(days=start_day_offset), time()) + timedelta(
        seconds=start_clock
    )
    # fold=0 takes the first of two equal clock times in October and moves a clock time that
    # doesn't exist in March an hour forward. Either way the two folds disagree.
    first = wall.replace(tzinfo=LOCAL)
    corrected = first.utcoffset() != wall.replace(tzinfo=LOCAL, fold=1).utcoffset()
    return first.astimezone(UTC), corrected


def load_plan(timetables_dir: Path, line: LineName, day: date) -> Plan:
    headers: list[TimetableHeader] = []
    for entry in _read_index(timetables_dir / INDEX_NAME):
        if entry.operator != line.operator:
            continue
        path = timetables_dir / entry.file
        if not path.is_file():
            raise TimetableError(entry.file, f"is in {INDEX_NAME} but missing")
        headers.append(read_header(path))
    chosen = choose_files(headers, line.operator, day)
    if not chosen:
        raise NoTimetableError(line.operator, day)
    files = [read_timetable(timetables_dir / line.operator / h.file_name) for h in chosen]
    trips = planned_trips(files, line, day)
    return Plan(tuple(h.file_name for h in chosen), tuple(trips))


def _read_index(path: Path) -> list[IndexEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TimetableError(INDEX_NAME, f"cannot read: {exc}") from None
    # The last line has no newline yet while the archiver is writing it.
    lines = text.split("\n")[:-1]
    entries = []
    for number, line in enumerate(lines, start=1):
        try:
            entries.append(IndexEntry.from_json(line))
        except (ValueError, KeyError, TypeError) as exc:
            raise TimetableError(INDEX_NAME, f"line {number} is invalid: {exc}") from None
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ov_reliability.timetables.plan",
        description="Print a line's planned trips for one day.",
    )
    parser.add_argument("operator", help='timetable archive directory, e.g. "htm"')
    parser.add_argument("mode", help='NeTEx transport mode, e.g. "tram"')
    parser.add_argument("code", help='public line code, e.g. "3"')
    parser.add_argument("day", type=date.fromisoformat, help="operating day, YYYY-MM-DD")
    parser.add_argument("--journey", type=int, help="print every stop of this journey")
    args = parser.parse_args(argv)
    # Output goes to stdout; warnings and errors to stderr, so they don't mix with it.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    path = config_path()
    try:
        settings = load_timetables_config(path)
    except (OSError, ValueError) as exc:
        print(f"Cannot use configuration {path}: {exc}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    line = LineName(args.operator, args.mode, args.code)
    try:
        plan = load_plan(settings.directory, line, args.day)
    except (TimetableError, NoTimetableError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_NO_PLAN

    if args.journey is None:
        _print_trips(line, args.day, plan)
        return 0
    trips = [trip for trip in plan.trips if trip.journey_number == args.journey]
    if not trips:
        print(f"Journey {args.journey} is not planned on {args.day}", file=sys.stderr)
        return EXIT_NO_PLAN
    for trip in trips:
        _print_stops(trip)
    return 0


def _print_trips(line: LineName, day: date, plan: Plan) -> None:
    print(f"{line.operator} {line.mode} {line.public_code}, {day}, files: {', '.join(plan.files)}")
    for trip in plan.trips:
        print(
            f"{_local(trip.start)}  journey {trip.journey_number:<7} pn {trip.planning_number:<4} "
            f"{trip.direction or '-':<9} {trip.destination or '-':<24} "
            f"{_local(trip.stops[0].departure)} -> {_local(trip.stops[-1].arrival)}  "
            f"{len(trip.stops)} stops"
        )
    totals = Counter(trip.direction or "no direction" for trip in plan.trips)
    print(
        ", ".join(f"{direction} {count} trips" for direction, count in totals.items()) or "no trips"
    )


def _print_stops(trip: PlannedTrip) -> None:
    print(
        f"journey {trip.journey_number}  pn {trip.planning_number}  {trip.direction or '-'}  "
        f"{trip.destination or '-'}"
    )
    for stop in trip.stops:
        print(
            f"{stop.sequence:>3}  {stop.stop_code:<8} {stop.stop_name:<32} "
            f"{_local(stop.arrival, seconds=True)}  {_local(stop.departure, seconds=True)}"
        )


def _local(at: datetime, seconds: bool = False) -> str:
    # The one place this command turns UTC into local time.
    return at.astimezone(LOCAL).strftime("%H:%M:%S" if seconds else "%H:%M")


if __name__ == "__main__":
    sys.exit(main())
