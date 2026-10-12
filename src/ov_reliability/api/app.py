"""The HTTP endpoints: served lines, and per line its trips and their summary."""

from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Protocol

from fastapi import FastAPI, HTTPException, Query

from ov_reliability.api import render
from ov_reliability.api.render import (
    LineOut,
    LinesResponse,
    Meta,
    MetaLine,
    RangeError,
    SummaryResponse,
    TripsResponse,
)
from ov_reliability.api.timetables import PlanCache
from ov_reliability.collector.records import Marker
from ov_reliability.config import ApiConfig
from ov_reliability.kv6.parse import TripKey
from ov_reliability.stats.gaps import Gap, gaps
from ov_reliability.stats.summary import summarise
from ov_reliability.stats.trips import Observations, Trip, build_trips
from ov_reliability.timetables.netex import LOCAL, TimetableFile
from ov_reliability.timetables.plan import (
    LineName,
    NoTimetableError,
    PlannedTrip,
    line_numbers,
    planned_trips,
)


class Store(Protocol):
    """What the API reads from the database; `queries.Reader` in production."""

    def observations(
        self, owner: str, planning_numbers: Collection[str], days: Collection[date]
    ) -> dict[TripKey, Observations]: ...

    def markers(self, recorder: str, start: datetime, end: datetime) -> list[Marker]: ...

    def data_until(self) -> dict[str, datetime]: ...


type Direction = Literal["inbound", "outbound"]

TIME_HELP = "ISO 8601 with a UTC offset; send '+' as %2B"


@dataclass(frozen=True)
class _Result:
    meta: Meta
    trips: list[Trip]


def create_app(
    settings: ApiConfig,
    connect: Callable[[], AbstractContextManager[Store]],
    plans: PlanCache,
    clock: Callable[[], datetime],
) -> FastAPI:
    app = FastAPI(
        title="ov-reliability",
        summary="Can I trust this line right now? Trips and delays of Dutch public transport.",
        version="1",
    )

    @app.get("/v1/lines")
    def lines() -> LinesResponse:
        today = clock().astimezone(LOCAL).date()
        found: list[LineOut] = []
        for operator in sorted({pattern.operator for pattern in settings.lines}):
            grouped: dict[tuple[str, str], dict[str, str]] = {}
            for timetable in _timetables(plans, operator, today):
                for number, line in timetable.lines.items():
                    if number in settings.ignored(operator):
                        continue
                    if settings.serves(operator, line.mode, line.public_code):
                        names = grouped.setdefault((line.mode, line.public_code), {})
                        names[number] = line.name
            for (mode, code), names in grouped.items():
                numbers = sorted(names)
                found.append(
                    LineOut(
                        operator=operator,
                        mode=mode,
                        code=code,
                        name=names[numbers[0]],
                        planning_numbers=numbers,
                        realtime=settings.realtime_for(operator, mode, code) is not None,
                    )
                )
        found.sort(key=lambda line: (line.operator, line.mode, _code_order(line.code)))
        return LinesResponse(lines=found)

    @app.get("/v1/lines/{operator}/{mode}/{code}/trips")
    def trips(
        operator: str,
        mode: str,
        code: str,
        start: Annotated[str | None, Query(alias="from", description=TIME_HELP)] = None,
        end: Annotated[str | None, Query(alias="to", description=TIME_HELP)] = None,
        direction: Direction | None = None,
    ) -> TripsResponse:
        result = _request(operator, mode, code, start, end, direction)
        return TripsResponse(meta=result.meta, trips=[render.trip_out(t) for t in result.trips])

    @app.get("/v1/lines/{operator}/{mode}/{code}/summary")
    def summary(
        operator: str,
        mode: str,
        code: str,
        start: Annotated[str | None, Query(alias="from", description=TIME_HELP)] = None,
        end: Annotated[str | None, Query(alias="to", description=TIME_HELP)] = None,
        direction: Direction | None = None,
    ) -> SummaryResponse:
        result = _request(operator, mode, code, start, end, direction)
        directions = [render.direction_out(block) for block in summarise(result.trips)]
        return SummaryResponse(meta=result.meta, directions=directions)

    def _request(
        operator: str,
        mode: str,
        code: str,
        start: str | None,
        end: str | None,
        direction: Direction | None,
    ) -> _Result:
        if not settings.serves(operator, mode, code):
            raise HTTPException(404, f"line {operator}/{mode}/{code} is not served")
        now = clock()
        try:
            since, until = render.parse_range(
                start,
                end,
                now,
                timedelta(minutes=settings.default_range_minutes),
                timedelta(days=settings.max_range_days),
            )
        except RangeError as exc:
            raise HTTPException(422, str(exc)) from None
        line = LineName(operator, mode, code)
        ignored = settings.ignored(operator)
        planned: list[PlannedTrip] = []
        numbers: set[str] = set()
        files: dict[str, list[str]] = {}
        for day in render.operating_days(since, until):
            timetables = [t for t in _timetables(plans, operator, day) if line_numbers(t, line)]
            files[day.isoformat()] = [t.header.file_name for t in timetables]
            numbers |= {n for t in timetables for n in line_numbers(t, line)}
            planned += [
                trip
                for trip in planned_trips(timetables, line, day)
                if trip.planning_number not in ignored
            ]
        if not numbers - ignored:
            raise HTTPException(404, f"line {operator}/{mode}/{code} is not in the timetable")

        source = settings.realtime_for(operator, mode, code)
        observations: dict[TripKey, Observations] = {}
        found_gaps: list[Gap] = []
        until_by_recorder: dict[str, datetime] = {}
        if source is not None:
            # Gaps matter over each trip's whole window, which can start before the range.
            gaps_from = min([since, *(trip.start for trip in planned)])
            with connect() as store:
                # Ignored planning numbers are read too: if they run, they show as unplanned.
                observations = store.observations(
                    operator.upper(), sorted(numbers), render.operating_days(since, until)
                )
                markers = store.markers(source.recorder, gaps_from, now)
                until_by_recorder = {
                    recorder: at
                    for recorder, at in store.data_until().items()
                    if recorder == source.recorder
                }
            found_gaps = gaps(source.recorder, markers, source.topics, gaps_from, now)
        found = build_trips(
            planned,
            observations,
            found_gaps,
            until_by_recorder.get(source.recorder) if source else None,
            now,
            since,
            until,
            timedelta(minutes=settings.missing_after_minutes),
            source is not None,
        )
        if direction is not None:
            found = [trip for trip in found if trip.direction == direction]
        in_range = [
            Gap(g.recorder, g.topic, max(g.start, since), min(g.end, until), g.reason)
            for g in found_gaps
            if g.start < until and g.end > since
        ]
        meta = Meta(
            line=MetaLine(
                operator=operator,
                mode=mode,
                code=code,
                planning_numbers=sorted(numbers - ignored),
            ),
            from_=render.local(since),
            to=render.local(until),
            generated_at=render.local(now),
            timetable_files=files,
            data_until={recorder: render.local(at) for recorder, at in until_by_recorder.items()},
            recorder_gaps=[render.gap_out(gap) for gap in in_range],
        )
        return _Result(meta, found)

    return app


def _timetables(plans: PlanCache, operator: str, day: date) -> list[TimetableFile]:
    try:
        return plans.timetables(operator, day)
    except NoTimetableError as exc:
        # Without a plan, "missing" and "not scheduled" can't be told apart.
        raise HTTPException(503, str(exc)) from None


def _code_order(code: str) -> tuple[int, str]:
    return (int(code), "") if code.isdigit() else (1 << 30, code)
