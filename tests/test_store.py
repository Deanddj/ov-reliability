import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import fresh_schema
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from kv6xml import Event, Journey, kv6_message, kv17_message
from sirixml import Call, Vj, siri_message

from ov_reliability.collector.archive import ArchiveWriter
from ov_reliability.collector.records import Marker, Message, Record
from ov_reliability.loader.batch import build_batch
from ov_reliability.loader.follow import Follower, Position
from ov_reliability.loader.store import (
    MigrationError,
    Progress,
    migrate,
    read_progress,
    reset_for_replay,
    write_batch,
)

TODAY = date(2026, 10, 9)
T = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
TABLES = (
    "stop_events",
    "trips_seen",
    "journey_mutations",
    "recorder_markers",
    "load_progress",
    "bad_messages",
)


def contents(conn: psycopg.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: sorted(map(tuple, conn.execute(f"SELECT * FROM {table}").fetchall()), key=repr)
        for table in TABLES
    }


def load(conn: psycopg.Connection, chunks: list[list[Record]], recorder: str = "collector") -> None:
    for i, chunk in enumerate(chunks):
        batch = build_batch(chunk, ["HTM"], TODAY, 90, None)
        until = chunk[-1].received_at if chunk else None
        write_batch(conn, recorder, batch, Position("f", i), until)


# Small value sets, so generated records repeat each other often, like EBS duplicates and
# repeated KV17 messages do.
events = st.builds(
    Event,
    kind=st.sampled_from(["ARRIVAL", "DEPARTURE", "ONSTOP"]),
    journey=st.sampled_from([1, 2]),
    stop=st.sampled_from(["A", "B"]),
    timestamp=st.sampled_from(["2026-10-09T10:00:00+02:00", "2026-10-09T10:01:00+02:00"]),
    punctuality=st.sampled_from(["0", "60"]),
)
kv6_records = st.lists(events, min_size=1, max_size=3).map(
    lambda items: Message(T, "/RIG/KV6posinfo", kv6_message(items))
)
kv17_records = st.builds(
    lambda journey, kind, at: Message(
        T, "/RIG/KV17cvlinfo", kv17_message(Journey(journey=journey), [(at, kind)])
    ),
    st.sampled_from([1, 2]),
    st.sampled_from(["CANCEL", "RECOVER"]),
    st.sampled_from(["2026-10-09T09:00:00", "2026-10-09T09:30:00"]),
)
markers = st.builds(
    lambda topic: Marker(T, "silence_start", {"silent_topic": topic}),
    st.sampled_from(["/RIG/KV6posinfo", "/GVB/KV6posinfo"]),
)
records = st.lists(st.one_of(kv6_records, kv17_records, markers), max_size=12)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(records=records, cut=st.integers(0, 12))
def test_loading_is_idempotent_and_split_independent(
    database_url: str, records: list[Record], cut: int
) -> None:
    with (
        fresh_schema(database_url) as once_url,
        fresh_schema(database_url) as split_url,
        psycopg.connect(once_url, autocommit=True) as once,
        psycopg.connect(split_url, autocommit=True) as split,
    ):
        load(once, [records])
        before = contents(once)
        load(once, [records])
        assert {k: v for k, v in contents(once).items() if k != "load_progress"} == {
            k: v for k, v in before.items() if k != "load_progress"
        }
        load(split, [records[:cut], records[cut:]])
        for table in ("stop_events", "trips_seen", "journey_mutations", "recorder_markers"):
            assert contents(split)[table] == before[table]


class CrashError(Exception):
    pass


def archive_of(directory: Path, count: int) -> None:
    writer = ArchiveWriter(directory, flush_seconds=5)
    for i in range(count):
        at = T + timedelta(minutes=7 * i)
        stamp = at.astimezone(UTC).isoformat()
        event = Event(journey=i % 5, stop=f"S{i % 3}", timestamp=stamp)
        writer.write(Message(at, "/RIG/KV6posinfo", kv6_message([event])))
    writer.close()


def run_loader(conn: psycopg.Connection, directory: Path, polls: int | None, crash_at: int) -> None:
    """Poll and write like the loader; stop after `polls` polls; fail one write mid-way."""
    progress = read_progress(conn, "collector")
    follower = Follower(directory, progress.position, max_records=3)
    done = 0
    while polls is None or done < polls:
        found, position = follower.poll()
        if not found:
            break
        batch = build_batch(found, ["HTM"], TODAY, 90, progress.floor)
        if done == crash_at:
            with pytest.raises(CrashError), conn.transaction():
                write_batch(conn, "collector", batch, position, found[-1].received_at)
                raise CrashError
            follower.close()
            return
        write_batch(conn, "collector", batch, position, found[-1].received_at)
        done += 1
    follower.close()


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(stops=st.lists(st.integers(0, 12), max_size=4), crash_at=st.integers(0, 12))
def test_restart_anywhere_loads_each_record_once(
    database_url: str, stops: list[int], crash_at: int
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        archive_of(directory, 30)  # five hourly files
        with (
            fresh_schema(database_url) as clean_url,
            fresh_schema(database_url) as broken_url,
            psycopg.connect(clean_url, autocommit=True) as clean,
            psycopg.connect(broken_url, autocommit=True) as broken,
        ):
            run_loader(clean, directory, None, crash_at=-1)
            run_loader(broken, directory, None, crash_at=crash_at)
            for polls in stops:
                run_loader(broken, directory, polls, crash_at=-1)
            run_loader(broken, directory, None, crash_at=-1)
            assert contents(broken) == contents(clean)
            [(count,)] = clean.execute("SELECT count(*) FROM stop_events").fetchall()
            assert count == 30


def test_rows_keep_their_source(database: psycopg.Connection) -> None:
    cancelled = Vj(
        owner="HTM",
        journey="2",
        cancelled=True,
        recorded=(),
        estimated=(Call(aimed="2026-10-09T10:30:00+02:00"),),
    )
    load(
        database,
        [
            [
                Message(T, "/RIG/KV6posinfo", kv6_message([Event()])),
                Message(
                    T,
                    "/RIG/KV17cvlinfo",
                    kv17_message(Journey(), [("2026-10-09T09:50", "CANCEL")]),
                ),
                Message(T, "/GVB/EstimatedTimetableDelivery", siri_message([Vj(owner="HTM")])),
                Message(T, "/GVB/EstimatedTimetableDelivery", siri_message([cancelled])),
            ]
        ],
    )
    events = database.execute("SELECT DISTINCT source FROM stop_events ORDER BY 1").fetchall()
    assert events == [("KV6",), ("SIRI",)]
    mutations = database.execute("SELECT DISTINCT source FROM journey_mutations ORDER BY 1")
    assert mutations.fetchall() == [("KV17",), ("SIRI",)]


def test_migrate_twice_is_a_no_op(database: psycopg.Connection) -> None:
    assert migrate(database) == []


def test_migration_gap_is_refused(database: psycopg.Connection, tmp_path: Path) -> None:
    (tmp_path / "001_init.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "003_more.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(MigrationError, match="without gaps"):
        migrate(database, tmp_path)


def test_unknown_applied_migration_is_refused(database: psycopg.Connection, tmp_path: Path) -> None:
    (tmp_path / "001_init.sql").write_text("SELECT 1", encoding="utf-8")
    database.execute("INSERT INTO schema_migrations (version) VALUES (3)")
    with pytest.raises(MigrationError, match="doesn't know"):
        migrate(database, tmp_path)


def test_progress_starts_before_the_oldest_file(database: psycopg.Connection) -> None:
    assert read_progress(database, "collector") == Progress(Position(None), None, None)


def test_replay_reset_keeps_days_before_the_floor(database: psycopg.Connection) -> None:
    def day_message(day: str) -> Message:
        return Message(T, "/RIG/KV6posinfo", kv6_message([Event(day=day)]))

    batch = build_batch(
        [day_message("2026-10-07"), day_message("2026-10-08"), day_message("2026-10-09")],
        ["HTM"],
        TODAY,
        90,
        None,
    )
    write_batch(database, "collector", batch, Position("f", 3), T)
    reset_for_replay(database, {"collector": T}, date(2026, 10, 8))
    days = database.execute("SELECT DISTINCT operating_day FROM stop_events").fetchall()
    assert days == [(date(2026, 10, 7),)]
    assert read_progress(database, "collector") == Progress(Position(None), date(2026, 10, 8), None)
