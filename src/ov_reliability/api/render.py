"""Turning UTC records into responses: local times with an offset, and the requested range."""

from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ov_reliability.stats.gaps import Gap
from ov_reliability.stats.summary import DirectionSummary
from ov_reliability.stats.trips import Status, Trip
from ov_reliability.timetables.netex import LOCAL

_EARLIEST = datetime(1900, 1, 1, tzinfo=UTC)
_LATEST = datetime(9000, 1, 1, tzinfo=UTC)


class RangeError(ValueError):
    pass


def local(at: datetime) -> str:
    """The one place a time is turned into Europe/Amsterdam (decision 054)."""
    if at.tzinfo is None:
        raise ValueError(f"naive time {at} can't be rendered")
    return at.astimezone(LOCAL).isoformat(timespec="seconds")


def parse_range(
    start: str | None,
    end: str | None,
    now: datetime,
    default: timedelta,
    longest: timedelta,
) -> tuple[datetime, datetime]:
    """The requested [from, to); without `to` it ends now, without `from` it is `default` long."""
    to = _parse_time("to", end) if end is not None else now
    since = _parse_time("from", start) if start is not None else to - default
    if since > to:
        raise RangeError("from is after to")
    if to - since > longest:
        raise RangeError(f"the range is longer than {longest.days} days")
    return since, to


def _parse_time(name: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # A "+" in a query string arrives as a space.
        hint = " (send '+' as %2B)" if " " in value else ""
        raise RangeError(f"{name} is not an ISO 8601 time: {value!r}{hint}") from None
    if parsed.tzinfo is None:
        # Never guessed (decision 054).
        raise RangeError(f"{name} has no UTC offset: {value!r}")
    # Far enough from the ends of the calendar that the range and its days can be computed.
    if not _EARLIEST <= parsed <= _LATEST:
        raise RangeError(f"{name} is out of range: {value!r}")
    return parsed


def operating_days(start: datetime, end: datetime) -> list[date]:
    """The operating days that can have a departure in [start, end).

    A trip can run past midnight, so the day before the range starts is included.
    """
    first = start.astimezone(LOCAL).date() - timedelta(days=1)
    last = max(end - timedelta(microseconds=1), start).astimezone(LOCAL).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, serialize_by_alias=True, validate_by_name=True)


class LineOut(_Model):
    operator: str
    mode: str
    code: str
    name: str
    planning_numbers: list[str]
    realtime: bool


class LinesResponse(_Model):
    lines: list[LineOut]


class MetaLine(_Model):
    operator: str
    mode: str
    code: str
    planning_numbers: list[str]


class GapOut(_Model):
    recorder: str
    topic: str | None
    from_: str = Field(alias="from")
    to: str
    reason: str


class Meta(_Model):
    line: MetaLine
    from_: str = Field(alias="from")
    to: str
    generated_at: str
    timetable_files: dict[str, list[str]]
    data_until: dict[str, str]
    recorder_gaps: list[GapOut]


class DepartureOut(_Model):
    stop_code: str
    stop_name: str | None
    sequence: int | None
    planned: str | None
    actual: str | None
    delay_seconds: int | None
    event: Literal["DEPARTURE", "ARRIVAL"] | None
    cancelled: bool


class TripOut(_Model):
    operating_day: date
    planning_number: str
    journey_number: int
    planned: bool
    direction: str | None
    destination: str | None
    planned_start: str | None
    status: Status
    departures: list[DepartureOut]


class TripsResponse(_Model):
    meta: Meta
    trips: list[TripOut]


class TripCountsOut(_Model):
    total: int
    ran: int
    cancelled: int
    missing: int
    unknown: int
    not_yet_due: int
    unplanned: int


class DepartureCountsOut(_Model):
    total: int
    with_delay: int
    without_delay: int
    cancelled: int


class PercentilesOut(_Model):
    p10: int | None
    p50: int | None
    p90: int | None


class DirectionOut(_Model):
    direction: str | None
    trips: TripCountsOut
    departures: DepartureCountsOut
    delay_seconds: PercentilesOut


class SummaryResponse(_Model):
    meta: Meta
    directions: list[DirectionOut]


def gap_out(gap: Gap) -> GapOut:
    return GapOut(
        recorder=gap.recorder,
        topic=gap.topic,
        from_=local(gap.start),
        to=local(gap.end),
        reason=gap.reason,
    )


def trip_out(trip: Trip) -> TripOut:
    return TripOut(
        operating_day=trip.operating_day,
        planning_number=trip.planning_number,
        journey_number=trip.journey_number,
        planned=trip.planned,
        direction=trip.direction,
        destination=trip.destination,
        planned_start=_maybe_local(trip.planned_start),
        status=trip.status,
        departures=[
            DepartureOut(
                stop_code=d.stop_code,
                stop_name=d.stop_name,
                sequence=d.sequence,
                planned=_maybe_local(d.planned),
                actual=_maybe_local(d.actual),
                delay_seconds=d.delay_seconds,
                event=d.event,
                cancelled=d.cancelled,
            )
            for d in trip.departures
        ],
    )


def direction_out(summary: DirectionSummary) -> DirectionOut:
    return DirectionOut(
        direction=summary.direction,
        trips=TripCountsOut(**vars(summary.trips)),
        departures=DepartureCountsOut(**vars(summary.departures)),
        delay_seconds=PercentilesOut(**vars(summary.delay_seconds)),
    )


def _maybe_local(at: datetime | None) -> str | None:
    return local(at) if at is not None else None
