"""One real hour of HTM tram 3 (Thursday 2026-10-08, 21:00-22:00), cut from the VPS."""

import json
from datetime import date, datetime

from conftest import FIXTURES

from ov_reliability.kv6.parse import Mutation, StopEvent, TripKey
from ov_reliability.stats.trips import Observations
from ov_reliability.timetables.plan import PlannedStop, PlannedTrip

DAY = date(2026, 10, 8)
DIRECTORY = FIXTURES / "tram3_hour"


def key(journey: int) -> TripKey:
    return TripKey(DAY, "HTM", "3", journey, 0)


def planned() -> list[PlannedTrip]:
    found = json.loads((DIRECTORY / "planned.json").read_text(encoding="utf-8"))
    return [
        PlannedTrip(
            data_owner="HTM",
            planning_number=trip["planning_number"],
            public_code="3",
            mode="tram",
            operating_day=DAY,
            journey_number=trip["journey_number"],
            direction=trip["direction"],
            destination=trip["destination"],
            stops=tuple(
                PlannedStop(seq, code, name, datetime.fromisoformat(a), datetime.fromisoformat(d))
                for seq, code, name, a, d in trip["stops"]
            ),
        )
        for trip in found
    ]


def observed() -> dict[TripKey, Observations]:
    rows = json.loads((DIRECTORY / "observed.json").read_text(encoding="utf-8"))
    events: dict[TripKey, list[StopEvent]] = {}
    for journey, stop, passage, event, at, punctuality, received in rows["stop_events"]:
        events.setdefault(key(journey), []).append(
            StopEvent(
                key(journey),
                stop,
                passage,
                event,
                datetime.fromisoformat(at),
                punctuality,
                datetime.fromisoformat(received),
            )
        )
    mutations: dict[TripKey, list[Mutation]] = {}
    for journey, kind, stop, passage, at, received in rows["mutations"]:
        mutations.setdefault(key(journey), []).append(
            Mutation(
                key(journey),
                kind,
                stop,
                passage,
                datetime.fromisoformat(at),
                datetime.fromisoformat(received),
            )
        )
    seen = {
        key(journey): (datetime.fromisoformat(first), datetime.fromisoformat(last))
        for journey, first, last in rows["trips_seen"]
    }
    return {
        k: Observations(tuple(events.get(k, ())), tuple(mutations.get(k, ())), seen.get(k))
        for k in events.keys() | mutations.keys() | seen.keys()
    }
