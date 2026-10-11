from datetime import UTC, date, datetime

import psycopg
import pytest

from ov_reliability.loader.partitions import (
    drop_partitions,
    ensure_partitions,
    existing_partitions,
    truncate_partitions,
    try_lock,
)

DAYS = [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]


def test_partitions_are_created_once(database: psycopg.Connection) -> None:
    ensure_partitions(database, DAYS)
    ensure_partitions(database, DAYS[1:])
    assert existing_partitions(database) == {
        "stop_events": DAYS,
        "trips_seen": DAYS,
        "journey_mutations": DAYS,
    }


def test_drop_removes_only_older_days(database: psycopg.Connection) -> None:
    ensure_partitions(database, DAYS)
    database.execute(
        "INSERT INTO recorder_markers VALUES ('collector', 'started', %s, '{}'), "
        "('collector', 'started', %s, '{}')",
        (datetime(2026, 10, 7, 23, tzinfo=UTC), datetime(2026, 10, 8, 0, tzinfo=UTC)),
    )
    dropped = drop_partitions(database, date(2026, 10, 8))
    assert sorted(dropped) == [
        "journey_mutations_20261007",
        "stop_events_20261007",
        "trips_seen_20261007",
    ]
    assert existing_partitions(database)["stop_events"] == DAYS[1:]
    [(count,)] = database.execute("SELECT count(*) FROM recorder_markers").fetchall()
    assert count == 1


def test_truncate_empties_days_from_the_first_on(database: psycopg.Connection) -> None:
    ensure_partitions(database, DAYS)
    assert len(truncate_partitions(database, date(2026, 10, 8))) == 6
    assert truncate_partitions(database, date(2026, 10, 10)) == []


def test_unexpected_partition_name_is_an_error(database: psycopg.Connection) -> None:
    database.execute(
        "CREATE TABLE stop_events_x PARTITION OF stop_events "
        "FOR VALUES FROM ('2026-01-01') TO ('2026-01-02')"
    )
    with pytest.raises(ValueError, match="stop_events_x"):
        existing_partitions(database)


def test_lock_is_held_by_one_connection(database: psycopg.Connection, database_url: str) -> None:
    assert try_lock(database)
    with psycopg.connect(database_url, autocommit=True) as other:
        assert not try_lock(other)
    database.execute("SELECT pg_advisory_unlock_all()")
