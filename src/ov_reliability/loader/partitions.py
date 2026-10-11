"""Day partitions, the window, and the lock that keeps one writer at a time."""

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta

import psycopg
from psycopg import sql

DAY_TABLES = ("stop_events", "trips_seen", "journey_mutations")
# One advisory lock id for the loader and replay, so they never write at the same time.
LOCK_ID = 4_171_017


def try_lock(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()
    return bool(row and row[0])


def partition_name(table: str, day: date) -> str:
    return f"{table}_{day:%Y%m%d}"


def existing_partitions(conn: psycopg.Connection) -> dict[str, list[date]]:
    rows = conn.execute(
        """
        SELECT parent.relname, child.relname
        FROM pg_inherits
        JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
        JOIN pg_class child ON child.oid = pg_inherits.inhrelid
        WHERE parent.relnamespace = current_schema()::regnamespace
        """
    ).fetchall()
    found: dict[str, list[date]] = {table: [] for table in DAY_TABLES}
    for parent, child in rows:
        if parent not in found:
            continue
        try:
            day = datetime.strptime(child.removeprefix(f"{parent}_"), "%Y%m%d").date()
        except ValueError:
            raise ValueError(f"unexpected partition {child!r} of {parent}") from None
        found[parent].append(day)
    return {table: sorted(days) for table, days in found.items()}


def ensure_partitions(conn: psycopg.Connection, days: Iterable[date]) -> None:
    wanted = set(days)
    if not wanted:
        return
    # Look first: creating a partition locks the parent table, which would block the API's reads.
    existing = existing_partitions(conn)
    for day in sorted(wanted):
        for table in DAY_TABLES:
            if day in existing[table]:
                continue
            conn.execute(
                sql.SQL("CREATE TABLE {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})").format(
                    sql.Identifier(partition_name(table, day)),
                    sql.Identifier(table),
                    sql.Literal(day),
                    sql.Literal(day + timedelta(days=1)),
                )
            )


def drop_partitions(conn: psycopg.Connection, before: date) -> list[str]:
    """Drop every day partition for days before `before`; delete markers and bad messages too."""
    dropped: list[str] = []
    for table, days in existing_partitions(conn).items():
        for day in days:
            if day < before:
                name = partition_name(table, day)
                conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(name)))
                dropped.append(name)
    cutoff = datetime.combine(before, time(), UTC)
    conn.execute("DELETE FROM recorder_markers WHERE at < %s", (cutoff,))
    conn.execute("DELETE FROM bad_messages WHERE received_at < %s", (cutoff,))
    return dropped


def truncate_partitions(conn: psycopg.Connection, first: date) -> list[str]:
    """Empty every day partition for days from `first` on."""
    names = [
        partition_name(table, day)
        for table, days in existing_partitions(conn).items()
        for day in days
        if day >= first
    ]
    if names:
        conn.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(map(sql.Identifier, names))))
    return names
