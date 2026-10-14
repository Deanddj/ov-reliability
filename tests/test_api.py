import copy
import json
import re
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES, config_data
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from netexfile import NAME, index_line, write_netex

from ov_reliability.api.app import create_app
from ov_reliability.api.timetables import PlanCache
from ov_reliability.collector.records import Marker
from ov_reliability.config import ApiConfig
from ov_reliability.kv6.parse import Mutation, StopEvent, TripKey
from ov_reliability.stats.summary import summarise
from ov_reliability.stats.trips import Departure, Observations, Trip
from ov_reliability.timetables.netex import LOCAL

# Wednesday 7 October, 08:00 in Den Haag.
NOW = datetime(2026, 10, 7, 8, 0, tzinfo=LOCAL).astimezone(UTC)
DAY = date(2026, 10, 7)
OFFSET = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
TIME_FIELDS = {"from", "to", "generated_at", "planned_start", "planned", "actual"}


def local(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=LOCAL).astimezone(UTC)


def key(journey: int, planning_number: str = "3") -> TripKey:
    return TripKey(DAY, "HTM", planning_number, journey, 0)


@dataclass
class FakeStore:
    observed: dict[TripKey, Observations] = field(default_factory=dict)
    recorder_markers: list[Marker] = field(
        default_factory=lambda: [Marker(local(7, 0, 0), "started", {"topics": ["/RIG/"]})]
    )
    loaded_until: dict[str, datetime] = field(default_factory=lambda: {"collector": NOW})
    calls: list[str] = field(default_factory=list)

    def observations(
        self, owner: str, planning_numbers: Collection[str], days: Collection[date]
    ) -> dict[TripKey, Observations]:
        self.calls.append(f"observations {owner} {sorted(planning_numbers)} {sorted(days)}")
        return {
            k: v
            for k, v in self.observed.items()
            if k.data_owner == owner and k.planning_number in planning_numbers
        }

    def markers(self, recorder: str, start: datetime, end: datetime) -> list[Marker]:
        self.calls.append(f"markers {recorder}")
        return [m for m in self.recorder_markers if m.received_at <= end]

    def data_until(self) -> dict[str, datetime]:
        return self.loaded_until


def api_settings(**changes: Any) -> ApiConfig:
    data = copy.deepcopy(config_data(Path("/tmp/archive"))["api"])
    data["lines"] = ["htm/tram/3", "htm/tram/4"]
    data.update(changes)
    return ApiConfig.model_validate(data)


@pytest.fixture
def timetables(tmp_path: Path) -> Path:
    write_netex(tmp_path)
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{NAME}"))
    return tmp_path


def make_app(
    timetables: Path,
    store: FakeStore | None = None,
    now: datetime = NOW,
    **changes: Any,
) -> FastAPI:
    used = store or FakeStore()

    @contextmanager
    def connect() -> Iterator[FakeStore]:
        yield used

    return create_app(api_settings(**changes), connect, PlanCache(timetables), lambda: now)


def client(
    timetables: Path, store: FakeStore | None = None, now: datetime = NOW, **changes: Any
) -> TestClient:
    return TestClient(make_app(timetables, store, now, **changes))


def get(test_client: TestClient, path: str, **params: str) -> Any:
    response = test_client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_lines(timetables: Path) -> None:
    body = get(client(timetables), "/v1/lines")
    assert body == {
        "lines": [
            {
                "operator": "htm",
                "mode": "tram",
                "code": "3",
                "name": "Zoetermeer - Loosduinen",
                "planning_numbers": ["3", "53"],
                "realtime": True,
            },
            {
                "operator": "htm",
                "mode": "tram",
                "code": "4",
                "name": "Lansingerland - Uithof",
                "planning_numbers": ["4"],
                "realtime": True,
            },
        ]
    }


def test_an_operator_can_be_part_of_another(timetables: Path) -> None:
    # The same file under another series: read too, its planning numbers would clash.
    bus = "NeTEx_HTM_Bus_20261001_20261005_new.xml.gz"
    write_netex(timetables, name=bus)
    with (timetables / "index.jsonl").open("a") as index:
        index.write(index_line(f"htm/{bus}"))
    store = FakeStore()
    test_client = client(
        timetables,
        store,
        lines=["haag/tram/3"],
        operators={"haag": {"timetables": "htm", "series": ["Rail"]}},
        ignore_planning_numbers=["haag/53"],
        realtime=[{"lines": ["haag/*/*"], "recorder": "collector", "topics": ["/RIG/"]}],
    )
    lines = get(test_client, "/v1/lines")["lines"]
    assert [(line["operator"], line["code"], line["planning_numbers"]) for line in lines] == [
        ("haag", "3", ["3"])
    ]
    body = get(test_client, "/v1/lines/haag/tram/3/trips")
    assert set(map(tuple, body["meta"]["timetable_files"].values())) == {(NAME,)}
    assert body["meta"]["line"]["planning_numbers"] == ["3"]
    assert store.calls[0].startswith("observations HTM ['3', '53'] ")
    assert test_client.get("/v1/lines/htm/tram/3/trips").status_code == 404


def test_lines_leave_out_ignored_planning_numbers(timetables: Path) -> None:
    body = get(client(timetables, ignore_planning_numbers=["htm/53"]), "/v1/lines")
    assert body["lines"][0]["planning_numbers"] == ["3"]


def test_trips_in_the_default_range(timetables: Path) -> None:
    store = FakeStore(
        observed={
            key(53001, "53"): Observations(
                events=(
                    StopEvent(
                        key(53001, "53"),
                        "1001",
                        0,
                        "DEPARTURE",
                        local(7, 7, 0, 40),
                        40,
                        local(7, 7, 0, 41),
                    ),
                ),
                seen=(local(7, 6, 59), local(7, 7, 5)),
            )
        }
    )
    body = get(client(timetables, store), "/v1/lines/htm/tram/3/trips")
    meta = body["meta"]
    assert meta["line"] == {
        "operator": "htm",
        "mode": "tram",
        "code": "3",
        "planning_numbers": ["3", "53"],
    }
    assert (meta["from"], meta["to"], meta["generated_at"]) == (
        "2026-10-07T06:00:00+02:00",
        "2026-10-07T08:00:00+02:00",
        "2026-10-07T08:00:00+02:00",
    )
    assert meta["timetable_files"] == {"2026-10-06": [NAME], "2026-10-07": [NAME]}
    assert meta["data_until"] == {"collector": "2026-10-07T08:00:00+02:00"}
    assert meta["recorder_gaps"] == []
    [trip] = body["trips"]
    assert {k: v for k, v in trip.items() if k != "departures"} == {
        "operating_day": "2026-10-07",
        "planning_number": "53",
        "journey_number": 53001,
        "planned": True,
        "direction": "outbound",
        "destination": "Zoetermeer Centrum",
        "planned_start": "2026-10-07T07:00:00+02:00",
        "status": "ran",
    }
    assert trip["departures"][0] == {
        "stop_code": "1001",
        "stop_name": "Alpha",
        "sequence": 1,
        "planned": "2026-10-07T07:00:00+02:00",
        "actual": "2026-10-07T07:00:40+02:00",
        "delay_seconds": 40,
        "event": "DEPARTURE",
        "cancelled": False,
    }
    assert store.calls == [
        "observations HTM ['3', '53'] [datetime.date(2026, 10, 6), datetime.date(2026, 10, 7)]",
        "markers collector",
    ]


def test_statuses_and_gaps(timetables: Path) -> None:
    store = FakeStore(
        observed={
            key(3001): Observations(
                mutations=(
                    Mutation(key(3001), "CANCEL", None, None, local(7, 5, 0), local(7, 5, 0)),
                )
            ),
        },
        recorder_markers=[
            Marker(local(7, 0, 0), "started", {"topics": ["/RIG/"]}),
            Marker(local(7, 7, 10), "disconnected", {}),
            Marker(local(7, 7, 12), "connected", {}),
        ],
    )
    body = get(
        client(timetables, store),
        "/v1/lines/htm/tram/3/trips",
        **{"from": "2026-10-07T05:00:00+02:00", "to": "2026-10-07T08:00:00+02:00"},
    )
    assert [(t["journey_number"], t["status"]) for t in body["trips"]] == [
        (3001, "cancelled"),
        (53001, "unknown"),
    ]
    assert body["meta"]["recorder_gaps"] == [
        {
            "recorder": "collector",
            "topic": None,
            "from": "2026-10-07T07:10:00+02:00",
            "to": "2026-10-07T07:12:00+02:00",
            "reason": "disconnected",
        }
    ]


def test_a_gap_before_the_range_still_counts_for_the_trip(timetables: Path) -> None:
    # Trip 53001 runs 07:00-07:05; its window reaches back before the range.
    store = FakeStore(
        recorder_markers=[
            Marker(local(7, 0, 0), "started", {"topics": ["/RIG/"]}),
            Marker(local(7, 6, 58), "disconnected", {}),
            Marker(local(7, 7, 1), "connected", {}),
        ]
    )
    body = get(
        client(timetables, store),
        "/v1/lines/htm/tram/3/trips",
        **{"from": "2026-10-07T07:04:00+02:00", "to": "2026-10-07T08:00:00+02:00"},
    )
    assert [(t["journey_number"], t["status"]) for t in body["trips"]] == [(53001, "unknown")]
    assert body["meta"]["recorder_gaps"] == []


def test_no_realtime_carves_a_line_out_of_a_realtime_entry(timetables: Path) -> None:
    changes: dict[str, Any] = {"lines": ["htm/tram/*"], "no_realtime": ["htm/tram/4"]}
    lines = get(client(timetables, **changes), "/v1/lines")["lines"]
    assert [(line["code"], line["realtime"]) for line in lines] == [("3", True), ("4", False)]
    store = FakeStore()
    get(client(timetables, store, **changes), "/v1/lines/htm/tram/4/trips")
    assert store.calls == []


def test_missing_and_not_yet_due(timetables: Path) -> None:
    body = get(
        client(timetables, now=local(7, 7, 10)),
        "/v1/lines/htm/tram/3/trips",
        **{"from": "2026-10-07T06:50:00+02:00"},
    )
    assert [t["status"] for t in body["trips"]] == ["not_yet_due"]
    body = get(
        client(timetables, now=local(7, 7, 30)),
        "/v1/lines/htm/tram/3/trips",
        **{"from": "2026-10-07T06:50:00+02:00"},
    )
    assert [t["status"] for t in body["trips"]] == ["missing"]


def test_ignored_planning_number_runs_as_unplanned(timetables: Path) -> None:
    store = FakeStore(
        observed={key(53001, "53"): Observations(seen=(local(7, 7, 0), local(7, 7, 5)))}
    )
    body = get(
        client(timetables, store, ignore_planning_numbers=["htm/53"]),
        "/v1/lines/htm/tram/3/trips",
    )
    assert body["meta"]["line"]["planning_numbers"] == ["3"]
    assert [(t["journey_number"], t["planned"], t["status"]) for t in body["trips"]] == [
        (53001, False, "ran")
    ]
    assert "['3', '53']" in store.calls[0]


def test_line_without_realtime_data(timetables: Path) -> None:
    store = FakeStore()
    realtime = copy.deepcopy(api_settings().model_dump()["realtime"])
    realtime[0]["lines"] = ["htm/tram/3"]
    changes = {"no_realtime": ["htm/tram/4"], "realtime": realtime}
    body = get(
        client(timetables, store, **changes),
        "/v1/lines/htm/tram/4/trips",
        **{"from": "2026-10-07T07:00:00+02:00", "to": "2026-10-07T09:00:00+02:00"},
    )
    assert store.calls == []
    assert body["meta"]["data_until"] == {}
    assert [(t["journey_number"], t["status"]) for t in body["trips"]] == [(4001, "not_yet_due")]
    lines = get(client(timetables, **changes), "/v1/lines")["lines"]
    assert [line["realtime"] for line in lines] == [True, False]


def test_direction_filter(timetables: Path) -> None:
    params = {"from": "2026-10-07T05:00:00+02:00", "to": "2026-10-08T00:00:00+02:00"}
    test_client = client(timetables)
    outbound = get(test_client, "/v1/lines/htm/tram/3/trips", direction="outbound", **params)
    inbound = get(test_client, "/v1/lines/htm/tram/3/trips", direction="inbound", **params)
    assert [t["journey_number"] for t in outbound["trips"]] == [3001, 53001]
    assert [t["journey_number"] for t in inbound["trips"]] == [3002]


def test_a_day_without_trips_is_empty(timetables: Path) -> None:
    body = get(
        client(timetables),
        "/v1/lines/htm/tram/4/trips",
        **{"from": "2026-10-07T10:00:00+02:00", "to": "2026-10-07T12:00:00+02:00"},
    )
    assert body["trips"] == []
    assert body["meta"]["line"]["planning_numbers"] == ["4"]


@pytest.mark.parametrize(
    ("path", "status", "detail"),
    [
        ("/v1/lines/htm/bus/3/trips", 404, "line htm/bus/3 is not served"),
        ("/v1/lines/ret/tram/3/summary", 404, "line ret/tram/3 is not served"),
        ("/v1/lines/htm/tram/99/trips", 404, "line htm/tram/99 is not in the timetable"),
        ("/v1/lines/htm/tram/3/trips?from=2026-10-07T06:00:00", 422, "from has no UTC offset"),
        (
            "/v1/lines/htm/tram/3/trips?to=2026-10-07T06:00:00+02:00",
            422,
            "to is not an ISO 8601 time: '2026-10-07T06:00:00 02:00' (send '+' as %2B)",
        ),
        ("/v1/lines/htm/tram/3/trips?to=yesterday", 422, "to is not an ISO 8601 time"),
        (
            "/v1/lines/htm/tram/3/trips?from=2026-10-07T07:00:00Z&to=2026-10-07T06:00:00Z",
            422,
            "from is after to",
        ),
        (
            "/v1/lines/htm/tram/3/summary?from=2026-09-30T00:00:00Z&to=2026-10-07T00:00:01Z",
            422,
            "the range is longer than 7 days",
        ),
        ("/v1/lines/htm/tram/3/trips?from=0001-01-01T00:00:00Z", 422, "from is out of range"),
        ("/v1/lines/htm/tram/3/trips?to=9999-12-31T23:59:59Z", 422, "to is out of range"),
        (
            "/v1/lines/htm/tram/3/trips?from=2026-10-12T06:00:00Z&to=2026-10-12T07:00:00Z",
            503,
            "no timetable file of htm covers 2026-10-12",
        ),
    ],
)
def test_errors(timetables: Path, path: str, status: int, detail: str) -> None:
    response = client(timetables, lines=["htm/tram/*"]).get(path)
    assert response.status_code == status
    assert response.json()["detail"].startswith(detail)


def test_unknown_direction_is_refused(timetables: Path) -> None:
    response = client(timetables).get("/v1/lines/htm/tram/3/trips?direction=north")
    assert response.status_code == 422


def _times(value: Any, name: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _times(v, k)
    elif isinstance(value, list):
        for item in value:
            yield from _times(item, name)
    elif name in TIME_FIELDS and isinstance(value, str):
        yield name, value


def _trip_of(body: dict[str, Any]) -> Trip:
    departures = tuple(
        Departure(
            d["stop_code"],
            d["stop_name"],
            d["sequence"],
            None,
            None,
            d["delay_seconds"],
            d["event"],
            d["cancelled"],
        )
        for d in body["departures"]
    )
    return Trip(
        date.fromisoformat(body["operating_day"]),
        body["planning_number"],
        body["journey_number"],
        body["planned"],
        body["direction"],
        body["destination"],
        None,
        body["status"],
        departures,
    )


def _observed(minutes: list[int]) -> dict[TripKey, Observations]:
    found: dict[TripKey, Observations] = {}
    for i, minute in enumerate(minutes):
        journey = (3001, 53001, 3002, 9999)[i % 4]
        number = "53" if journey == 53001 else "3"
        at = local(7, 5, 0) + timedelta(minutes=minute)
        trip = key(journey, number)
        event = StopEvent(trip, "1001", 0, "DEPARTURE", at, minute * 7 - 300, at)
        old = found.get(trip, Observations())
        found[trip] = Observations((*old.events, event), (), (at, at))
    return found


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.integers(0, 60 * 24),
    st.integers(0, 60 * 30),
    st.lists(st.integers(0, 60 * 20), max_size=6),
    st.integers(-60, 60 * 24),
)
def test_responses_follow_the_contract(
    timetables: Path, start: int, length: int, minutes: list[int], now_minute: int
) -> None:
    since = local(6, 22, 0) + timedelta(minutes=start)
    params = {
        "from": since.isoformat(),
        "to": (since + timedelta(minutes=length)).isoformat(),
    }
    now = local(7, 0, 0) + timedelta(minutes=now_minute)
    test_client = client(timetables, FakeStore(observed=_observed(minutes)), now=now)
    trips = get(test_client, "/v1/lines/htm/tram/3/trips", **params)
    summary = get(test_client, "/v1/lines/htm/tram/3/summary", **params)
    assert summary["meta"] == trips["meta"]
    expected = summarise([_trip_of(trip) for trip in trips["trips"]])
    assert [block["direction"] for block in summary["directions"]] == [
        block.direction for block in expected
    ]
    for block, wanted in zip(summary["directions"], expected, strict=True):
        assert block["trips"] == vars(wanted.trips)
        assert block["departures"] == vars(wanted.departures)
        assert block["delay_seconds"] == vars(wanted.delay_seconds)
    for name, value in _times(trips):
        assert OFFSET.match(value), (name, value)
    start_at = datetime.fromisoformat(trips["meta"]["from"])
    end_at = datetime.fromisoformat(trips["meta"]["to"])
    for trip in trips["trips"]:
        for departure in trip["departures"]:
            if departure["planned"] is not None:
                assert start_at <= datetime.fromisoformat(departure["planned"]) < end_at


def test_openapi_snapshot(timetables: Path) -> None:
    # A change to the contract fails here until the snapshot is updated in the same change.
    found = make_app(timetables).openapi()
    assert found == json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))
