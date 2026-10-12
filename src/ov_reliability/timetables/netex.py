"""Read one archived NeTEx timetable file into compact data."""

import gzip
import math
import re
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo("Europe/Amsterdam")

_N = "{http://www.netex.org.uk/netex}"
_FILE_NAME = re.compile(
    r"^NeTEx_(?P<operator>[A-Za-z0-9]+)_(?P<series>[A-Za-z0-9.\-]+)_.+\.xml\.gz$"
)
_DURATION = re.compile(r"PT(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?")
_CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d):([0-5]\d)")
_STOP = _N + "StopPointInJourneyPattern"
_TIMING = _N + "TimingPointInJourneyPattern"
_KEPT = frozenset(
    _N + tag
    for tag in (
        "PublicationTimestamp",
        "ValidBetween",
        "Line",
        "Route",
        "DestinationDisplay",
        "ScheduledStopPoint",
        "ServiceJourneyPattern",
        "TimeDemandType",
        "AvailabilityCondition",
        "ServiceJourney",
    )
)


class TimetableError(Exception):
    def __init__(self, file: str, reason: str) -> None:
        super().__init__(f"{file}: {reason}")
        self.file = file
        self.reason = reason


@dataclass(frozen=True)
class TimetableHeader:
    operator: str
    series: str
    file_name: str
    published_at: datetime
    valid_from: date
    valid_to: date | None  # None: open-ended

    def covers(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True)
class Line:
    public_code: str
    mode: str
    name: str


@dataclass(frozen=True)
class Stop:
    sequence: int
    stop_code: str
    stop_name: str
    arrival_s: int  # seconds since the journey's start
    departure_s: int


@dataclass(frozen=True)
class StopProfile:
    direction: str | None
    destination: str | None
    stops: tuple[Stop, ...]


@dataclass(frozen=True)
class Journey:
    planning_number: str
    journey_number: int
    profile: int  # index into TimetableFile.profiles
    start_clock: int  # seconds after midnight of the start day
    start_day_offset: int
    days: frozenset[date]


@dataclass(frozen=True)
class TimetableFile:
    header: TimetableHeader
    lines: dict[str, Line]  # by planning number
    profiles: tuple[StopProfile, ...]
    journeys: tuple[Journey, ...]


class _ContentError(Exception):
    """Raised while reading; turned into TimetableError with the file name."""


def read_header(path: Path) -> TimetableHeader:
    operator, series = _name_parts(path)
    published_at: datetime | None = None
    try:
        for element in _elements(path):
            if element.tag == _N + "PublicationTimestamp":
                published_at = _timestamp(_required_text(element))
            elif element.tag == _N + "ValidBetween":
                if published_at is None:
                    break
                valid_from, valid_to = _validity(element)
                return TimetableHeader(
                    operator, series, path.name, published_at, valid_from, valid_to
                )
    except _ContentError as exc:
        raise TimetableError(path.name, str(exc)) from None
    raise TimetableError(path.name, "no PublicationTimestamp before the first ValidBetween")


def read_timetable(path: Path) -> TimetableFile:
    try:
        return _Reader(path).read()
    except _ContentError as exc:
        raise TimetableError(path.name, str(exc)) from None


@dataclass(frozen=True)
class _Point:
    ref: str  # ScheduledStopPoint or TimingPoint id
    is_stop: bool
    onward_link: str | None


@dataclass(frozen=True)
class _Pattern:
    route: str
    destination: str | None
    points: tuple[_Point, ...]


@dataclass(frozen=True)
class _RawJourney:
    number: int
    pattern: str
    time_demand: str
    condition: str
    start_clock: int
    start_day_offset: int


class _Reader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.published_at: datetime | None = None
        self.validity: tuple[date, date | None] | None = None
        self.lines: dict[str, tuple[str, Line]] = {}
        self.routes: dict[str, tuple[str, str | None]] = {}
        self.destinations: dict[str, str | None] = {}
        self.stops: dict[str, tuple[str, str]] = {}
        self.patterns: dict[str, _Pattern] = {}
        self.time_demands: dict[str, tuple[dict[str, Fraction], dict[str, Fraction]]] = {}
        self.conditions: dict[str, frozenset[date]] = {}
        self.journeys: list[_RawJourney] = []
        self.durations: dict[str, Fraction] = {}

    def read(self) -> TimetableFile:
        operator, series = _name_parts(self.path)
        handlers = {
            _N + "PublicationTimestamp": self._publication,
            _N + "ValidBetween": self._valid_between,
            _N + "Line": self._line,
            _N + "Route": self._route,
            _N + "DestinationDisplay": self._destination,
            _N + "ScheduledStopPoint": self._stop,
            _N + "ServiceJourneyPattern": self._pattern,
            _N + "TimeDemandType": self._time_demand,
            _N + "AvailabilityCondition": self._condition,
            _N + "ServiceJourney": self._journey,
        }
        for element in _elements(self.path):
            handlers[element.tag](element)
        if self.published_at is None or self.validity is None:
            raise _ContentError("no PublicationTimestamp or ValidBetween")
        header = TimetableHeader(
            operator, series, self.path.name, self.published_at, *self.validity
        )
        return self._resolve(header)

    def _publication(self, element: ET.Element) -> None:
        self.published_at = _timestamp(_required_text(element))

    def _valid_between(self, element: ET.Element) -> None:
        # The file's own validity comes first; later ones belong to single objects.
        if self.validity is None:
            self.validity = _validity(element)

    def _line(self, element: ET.Element) -> None:
        number = _private_code(element, "LinePlanningNumber")
        line = Line(
            public_code=_child_text(element, "PublicCode"),
            mode=_child_text(element, "TransportMode"),
            name=_child_text(element, "Name"),
        )
        self.lines[_id(element)] = (number, line)

    def _route(self, element: ET.Element) -> None:
        line_ref = _ref(_child(element, "LineRef"))
        self.routes[_id(element)] = (line_ref, element.findtext(_N + "DirectionType"))

    def _destination(self, element: ET.Element) -> None:
        self.destinations[_id(element)] = element.findtext(_N + "FrontText")

    def _stop(self, element: ET.Element) -> None:
        code = _private_code(element, "UserStopCode")
        self.stops[_id(element)] = (code, _child_text(element, "Name"))

    def _pattern(self, element: ET.Element) -> None:
        points: list[_Point] = []
        orders: list[str | None] = []
        for point in _child(element, "pointsInSequence"):
            if point.tag == _STOP:
                ref = _ref(_child(point, "ScheduledStopPointRef"))
            elif point.tag == _TIMING:
                ref = _ref(_child(point, "TimingPointRef"))
            else:
                raise _ContentError(f"pattern {_id(element)} has an unknown point {point.tag}")
            link = point.find(_N + "OnwardTimingLinkRef")
            points.append(_Point(ref, point.tag == _STOP, None if link is None else _ref(link)))
            orders.append(point.get("order"))
        pattern_id = _id(element)
        if orders and None not in orders:
            numbers = [_integer(order, "order") for order in orders if order is not None]
            if numbers != sorted(set(numbers)):
                raise _ContentError(f"pattern {pattern_id}: order differs from document order")
        destination = element.find(_N + "DestinationDisplayRef")
        self.patterns[pattern_id] = _Pattern(
            route=_ref(_child(element, "RouteRef")),
            destination=None if destination is None else _ref(destination),
            points=tuple(points),
        )

    def _time_demand(self, element: ET.Element) -> None:
        runs = {
            _ref(_child(run, "TimingLinkRef")): self._duration(_child_text(run, "RunTime"))
            for run in element.iter(_N + "JourneyRunTime")
        }
        waits: dict[str, Fraction] = {}
        for wait in element.iter(_N + "JourneyWaitTime"):
            point = wait.find(_N + "ScheduledStopPointRef")
            if point is None:
                point = _child(wait, "TimingPointRef")
            waits[_ref(point)] = self._duration(_child_text(wait, "WaitTime"))
        self.time_demands[_id(element)] = (runs, waits)

    def _condition(self, element: ET.Element) -> None:
        start = _date(_child_text(element, "FromDate"))
        bits = element.findtext(_N + "ValidDayBits") or ""
        self.conditions[_id(element)] = frozenset(
            start + timedelta(days=i) for i, bit in enumerate(bits) if bit == "1"
        )

    def _journey(self, element: ET.Element) -> None:
        match = _CLOCK.fullmatch(_child_text(element, "DepartureTime"))
        if match is None:
            raise _ContentError(f"journey {_id(element)}: bad DepartureTime")
        hours, minutes, seconds = (int(part) for part in match.groups())
        # NeTEx's default offset is 0; HTM, GVB and RET always write it.
        offset = element.findtext(_N + "DepartureDayOffset") or "0"
        condition = element.find(f"{_N}validityConditions/{_N}AvailabilityConditionRef")
        if condition is None:
            raise _ContentError(f"journey {_id(element)}: no AvailabilityConditionRef")
        self.journeys.append(
            _RawJourney(
                number=_integer(_private_code(element, "JourneyNumber"), "JourneyNumber"),
                pattern=_ref(_child(element, "ServiceJourneyPatternRef")),
                time_demand=_ref(_child(element, "TimeDemandTypeRef")),
                condition=_ref(condition),
                start_clock=hours * 3600 + minutes * 60 + seconds,
                start_day_offset=_integer(offset, "DepartureDayOffset"),
            )
        )

    def _duration(self, text: str) -> Fraction:
        # Many run times repeat, so equal texts share one value.
        if text not in self.durations:
            match = _DURATION.fullmatch(text)
            if match is None:
                raise _ContentError(f"bad duration {text!r}")
            hours, minutes, seconds = (Fraction(part or 0) for part in match.groups())
            self.durations[text] = hours * 3600 + minutes * 60 + seconds
        return self.durations[text]

    def _resolve(self, header: TimetableHeader) -> TimetableFile:
        lines: dict[str, Line] = {}
        for number, line in self.lines.values():
            if lines.setdefault(number, line) != line:
                raise _ContentError(f"planning number {number} has two different lines")
        profiles: dict[tuple[str, str], int] = {}
        profile_list: list[StopProfile] = []
        journeys: list[Journey] = []
        for raw in self.journeys:
            pattern = _lookup(self.patterns, raw.pattern, "pattern")
            line_ref, _ = _lookup(self.routes, pattern.route, "route")
            number, _ = _lookup(self.lines, line_ref, "line")
            key = (raw.pattern, raw.time_demand)
            if key not in profiles:
                profiles[key] = len(profile_list)
                profile_list.append(self._profile(raw.pattern, raw.time_demand))
            days = _lookup(self.conditions, raw.condition, "availability condition")
            journeys.append(
                Journey(
                    planning_number=number,
                    journey_number=raw.number,
                    profile=profiles[key],
                    start_clock=raw.start_clock,
                    start_day_offset=raw.start_day_offset,
                    days=frozenset(day for day in days if header.covers(day)),
                )
            )
        return TimetableFile(header, lines, tuple(profile_list), tuple(journeys))

    def _profile(self, pattern_id: str, time_demand_id: str) -> StopProfile:
        pattern = self.patterns[pattern_id]
        _, direction = self.routes[pattern.route]
        destination = None
        if pattern.destination is not None:
            destination = _lookup(self.destinations, pattern.destination, "destination")
        runs, waits = _lookup(self.time_demands, time_demand_id, "time demand type")
        stops: list[Stop] = []
        arrival = Fraction(0)
        for point in pattern.points:
            departure = arrival + waits.get(point.ref, 0)
            if point.is_stop:
                code, name = _lookup(self.stops, point.ref, "stop")
                stops.append(Stop(len(stops) + 1, code, name, _whole(arrival), _whole(departure)))
            if point.onward_link is not None:
                if point.onward_link not in runs:
                    raise _ContentError(
                        f"time demand type {time_demand_id} has no run time for {point.onward_link}"
                    )
                arrival = departure + runs[point.onward_link]
        if not stops:
            raise _ContentError(f"pattern {pattern_id} has no stops")
        return StopProfile(direction, destination, tuple(stops))


def _elements(path: Path) -> Iterator[ET.Element]:
    """Yields each kept element once it is complete; everything else is dropped as it ends."""
    try:
        with gzip.open(path) as file:
            stack: list[ET.Element] = []
            inside = 0
            for event, element in ET.iterparse(file, events=("start", "end")):
                if event == "start":
                    stack.append(element)
                    inside += element.tag in _KEPT
                    continue
                stack.pop()
                if element.tag in _KEPT:
                    inside -= 1
                    yield element
                # Files can be over 250 MB unpacked, so nothing is kept once it has been used.
                if inside == 0 and stack:
                    stack[-1].remove(element)
    except (OSError, EOFError, zlib.error, ET.ParseError) as exc:
        raise TimetableError(path.name, f"cannot read: {exc}") from None


def _name_parts(path: Path) -> tuple[str, str]:
    match = _FILE_NAME.match(path.name)
    if match is None or match["operator"].lower() != path.parent.name:
        raise TimetableError(
            path.name, f"not a NeTEx_<operator>_<series>_... file in {path.parent.name}/"
        )
    return path.parent.name, match["series"]


def _timestamp(text: str) -> datetime:
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        raise _ContentError(f"bad timestamp {text!r}") from None
    # Without an offset, times are in the frame's default time zone, which is local time.
    return at.replace(tzinfo=LOCAL) if at.tzinfo is None else at


def _validity(element: ET.Element) -> tuple[date, date | None]:
    # Only the vehicles files leave ToDate out, meaning open-ended.
    to_date = element.findtext(_N + "ToDate")
    return _date(_child_text(element, "FromDate")), None if to_date is None else _date(to_date)


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise _ContentError(f"bad date {text!r}") from None


def _integer(text: str, what: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise _ContentError(f"bad {what} {text!r}") from None


def _whole(seconds: Fraction) -> int:
    return math.floor(seconds + Fraction(1, 2))


def _child(element: ET.Element, tag: str) -> ET.Element:
    child = element.find(_N + tag)
    if child is None:
        raise _ContentError(f"{_short(element)} has no {tag}")
    return child


def _child_text(element: ET.Element, tag: str) -> str:
    return _required_text(_child(element, tag))


def _required_text(element: ET.Element) -> str:
    if not element.text or not element.text.strip():
        raise _ContentError(f"{_short(element)} is empty")
    return element.text.strip()


def _private_code(element: ET.Element, kind: str) -> str:
    for code in element.iter(_N + "PrivateCode"):
        if code.get("type") == kind:
            return _required_text(code)
    raise _ContentError(f"{_short(element)} has no {kind}")


def _id(element: ET.Element) -> str:
    value = element.get("id")
    if not value:
        raise _ContentError(f"{_short(element)} has no id")
    return value


def _ref(element: ET.Element) -> str:
    value = element.get("ref")
    if not value:
        raise _ContentError(f"{_short(element)} has no ref")
    return value


def _lookup[T](table: dict[str, T], key: str, what: str) -> T:
    if key not in table:
        raise _ContentError(f"{what} {key} does not exist")
    return table[key]


def _short(element: ET.Element) -> str:
    name = element.tag.removeprefix(_N)
    return f"{name} {element.get('id')}" if element.get("id") else name
