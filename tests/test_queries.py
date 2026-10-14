from datetime import UTC, date, datetime, timedelta

import psycopg

from ov_reliability.api.queries import EVENTS_QUERY, Reader
from ov_reliability.collector.records import Marker
from ov_reliability.kv6.parse import Mutation, StopEvent, TripKey
from ov_reliability.loader.batch import Batch, MarkerRow, SeenSpan
from ov_reliability.loader.follow import Position
from ov_reliability.loader.store import write_batch
from ov_reliability.stats.trips import Observations

DAY = date(2026, 10, 9)
T = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T + timedelta(minutes=minutes)


def trip(journey: int, planning_number: str = "3", day: date = DAY) -> TripKey:
    return TripKey(day, "HTM", planning_number, journey, 0)


def store(
    conn: psycopg.Connection,
    events: tuple[StopEvent, ...] = (),
    seen: tuple[SeenSpan, ...] = (),
    mutations: tuple[Mutation, ...] = (),
    marker_rows: tuple[MarkerRow, ...] = (),
    until: datetime | None = None,
    recorder: str = "collector",
) -> None:
    batch = Batch(
        tuple((e, "KV6") for e in events),
        seen,
        tuple((m, "KV17") for m in mutations),
        marker_rows,
        (),
    )
    write_batch(conn, recorder, batch, Position("f", 1), until)


def test_observations_come_back_per_trip(database: psycopg.Connection) -> None:
    departure = StopEvent(trip(1), "A", 0, "DEPARTURE", at(1), 42, at(1.1))
    arrival = StopEvent(trip(1), "B", 0, "ARRIVAL", at(3), 50, at(3.1))
    cancel = Mutation(trip(2), "CANCEL", None, None, at(-30), at(-29))
    shorten = Mutation(trip(1), "SHORTEN", "C", 0, at(-20), at(-19))
    other_line = StopEvent(trip(5, "4"), "A", 0, "DEPARTURE", at(1), 0, at(1))
    other_day = StopEvent(trip(1, day=date(2026, 10, 10)), "A", 0, "DEPARTURE", at(1), 0, at(1))
    store(
        database,
        events=(departure, arrival, other_line, other_day),
        seen=(SeenSpan(trip(1), at(0.5), at(3)), SeenSpan(trip(3, "53"), at(9), at(10))),
        mutations=(cancel, shorten),
    )
    found = Reader(database).observations("HTM", ["3", "53"], [DAY])
    assert found.keys() == {trip(1), trip(2), trip(3, "53")}
    assert sorted(found[trip(1)].events, key=lambda e: e.event_at) == [departure, arrival]
    assert found[trip(1)].mutations == (shorten,)
    assert found[trip(1)].seen == (at(0.5), at(3))
    assert found[trip(2)] == Observations(mutations=(cancel,))
    assert found[trip(3, "53")] == Observations(seen=(at(9), at(10)))
    assert Reader(database).observations("RET", ["3"], [DAY]) == {}


def test_stop_events_are_read_from_the_index_alone(database: psycopg.Connection) -> None:
    store(database, events=(StopEvent(trip(1), "A", 0, "DEPARTURE", at(1), 42, at(1.1)),))
    # A tiny table would be read whole; this asks whether the index can answer on its own.
    database.execute("SET enable_seqscan = off")
    database.execute("SET enable_bitmapscan = off")
    plan = [row[0] for row in database.execute("EXPLAIN " + EVENTS_QUERY, ("HTM", ["3"], [DAY]))]
    assert any("Index Only Scan" in line for line in plan), plan


def test_markers_start_at_the_last_start_before_the_range(database: psycopg.Connection) -> None:
    rows = (
        MarkerRow(at(-120), "started", {"topics": ["/RIG/"]}),
        MarkerRow(at(-100), "stopped", {"reason": "signal"}),
        MarkerRow(at(-90), "started", {"topics": ["/RIG/"]}),
        MarkerRow(at(-90), "connected", {}),
        MarkerRow(at(-50), "silence_start", {"silent_topic": "/RIG/KV6posinfo"}),
        MarkerRow(at(30), "disconnected", {}),
        MarkerRow(at(90), "connected", {}),
    )
    store(database, marker_rows=rows)
    store(database, marker_rows=(MarkerRow(at(10), "stopped", {}),), recorder="collector-siri")
    found = Reader(database).markers("collector", at(0), at(60))
    assert [(m.received_at, m.event) for m in found] == [
        (at(-90), "connected"),
        (at(-90), "started"),
        (at(-50), "silence_start"),
        (at(30), "disconnected"),
    ]
    assert found[2] == Marker(at(-50), "silence_start", {"silent_topic": "/RIG/KV6posinfo"})


def test_markers_without_an_earlier_start(database: psycopg.Connection) -> None:
    store(database, marker_rows=(MarkerRow(at(10), "started", {"topics": []}),))
    assert [m.received_at for m in Reader(database).markers("collector", at(0), at(60))] == [at(10)]
    assert Reader(database).markers("collector", at(0), at(5)) == []


def test_data_until_per_recorder(database: psycopg.Connection) -> None:
    assert Reader(database).data_until() == {}
    store(database, until=None)
    store(database, until=at(5), recorder="collector-siri")
    assert Reader(database).data_until() == {"collector-siri": at(5)}
