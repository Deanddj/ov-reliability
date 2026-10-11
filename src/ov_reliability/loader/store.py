"""Schema migrations, and writing batches to Postgres with the load position."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from ov_reliability.loader.batch import Batch
from ov_reliability.loader.follow import Position
from ov_reliability.loader.partitions import ensure_partitions, truncate_partitions

MIGRATIONS = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Progress:
    position: Position
    floor: date | None
    data_until: datetime | None


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS) -> list[int]:
    """Apply the migrations not applied yet, each in its own transaction. Returns their versions."""
    files: dict[int, Path] = {}
    for path in directory.glob("*.sql"):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise MigrationError(f"unexpected file in migrations: {path.name}")
        files[int(match[1])] = path
    if sorted(files) != list(range(1, len(files) + 1)):
        raise MigrationError(f"migration numbers must run 1..n without gaps: {sorted(files)}")
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    unknown = applied - files.keys()
    if unknown:
        raise MigrationError(f"database has migrations this code doesn't know: {sorted(unknown)}")
    done: list[int] = []
    for version in sorted(files.keys() - applied):
        with conn.transaction():
            conn.execute(files[version].read_text(encoding="utf-8").encode())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        done.append(version)
    return done


def read_progress(conn: psycopg.Connection, recorder: str) -> Progress:
    row = conn.execute(
        "SELECT file_name, records, floor, data_until FROM load_progress WHERE recorder = %s",
        (recorder,),
    ).fetchone()
    if row is None:
        return Progress(Position(None), None, None)
    file_name, records, floor, data_until = row
    return Progress(Position(file_name, records), floor, data_until)


def write_batch(
    conn: psycopg.Connection,
    recorder: str,
    batch: Batch,
    position: Position,
    data_until: datetime | None,
) -> None:
    """Store a batch and the position after it in one transaction: all of it or none."""
    with conn.transaction(), conn.cursor() as cur:
        ensure_partitions(conn, batch.days)
        if batch.events:
            cur.execute("CREATE TEMP TABLE new_stop_events (LIKE stop_events) ON COMMIT DROP")
            with cur.copy("COPY new_stop_events FROM STDIN") as copy:
                for e in batch.events:
                    t = e.trip
                    copy.write_row(
                        (
                            t.operating_day,
                            t.data_owner,
                            t.planning_number,
                            t.journey_number,
                            t.reinforcement,
                            e.stop_code,
                            e.passage,
                            e.event,
                            e.event_at,
                            e.punctuality,
                            e.received_at,
                            "KV6",
                        )
                    )
            cur.execute(
                "INSERT INTO stop_events SELECT * FROM new_stop_events ON CONFLICT DO NOTHING"
            )
        if batch.seen:
            cur.execute("CREATE TEMP TABLE new_trips_seen (LIKE trips_seen) ON COMMIT DROP")
            with cur.copy("COPY new_trips_seen FROM STDIN") as copy:
                for s in batch.seen:
                    t = s.trip
                    copy.write_row(
                        (
                            t.operating_day,
                            t.data_owner,
                            t.planning_number,
                            t.journey_number,
                            t.reinforcement,
                            s.first_at,
                            s.last_at,
                        )
                    )
            cur.execute(
                """
                INSERT INTO trips_seen SELECT * FROM new_trips_seen
                ON CONFLICT (operating_day, data_owner, planning_number, journey_number,
                             reinforcement)
                DO UPDATE SET
                    first_seen_at = LEAST(trips_seen.first_seen_at, EXCLUDED.first_seen_at),
                    last_seen_at = GREATEST(trips_seen.last_seen_at, EXCLUDED.last_seen_at)
                """
            )
        if batch.mutations:
            cur.execute(
                "CREATE TEMP TABLE new_journey_mutations (LIKE journey_mutations) ON COMMIT DROP"
            )
            with cur.copy("COPY new_journey_mutations FROM STDIN") as copy:
                for m in batch.mutations:
                    t = m.trip
                    copy.write_row(
                        (
                            t.operating_day,
                            t.data_owner,
                            t.planning_number,
                            t.journey_number,
                            t.reinforcement,
                            m.mutation,
                            m.stop_code,
                            m.passage,
                            m.mutated_at,
                            m.received_at,
                            "KV17",
                        )
                    )
            cur.execute(
                "INSERT INTO journey_mutations SELECT * FROM new_journey_mutations"
                " ON CONFLICT DO NOTHING"
            )
        if batch.markers:
            cur.executemany(
                "INSERT INTO recorder_markers (recorder, event, at, details)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [(recorder, m.event, m.at, Jsonb(m.details)) for m in batch.markers],
            )
        if batch.bad:
            cur.executemany(
                "INSERT INTO bad_messages (recorder, received_at, topic, reason)"
                " VALUES (%s, %s, %s, %s)",
                [(recorder, b.received_at, b.topic, b.reason) for b in batch.bad],
            )
        cur.execute(
            """
            INSERT INTO load_progress (recorder, file_name, records, data_until)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (recorder) DO UPDATE SET
                file_name = EXCLUDED.file_name,
                records = EXCLUDED.records,
                data_until = COALESCE(EXCLUDED.data_until, load_progress.data_until)
            """,
            (recorder, position.file_name, position.records, data_until),
        )


def reset_for_replay(
    conn: psycopg.Connection, oldest_hours: dict[str, datetime], floor: date
) -> list[str]:
    """Empty the days from `floor` on and send every recorder back to its oldest file."""
    with conn.transaction():
        truncated = truncate_partitions(conn, floor)
        for recorder, hour in oldest_hours.items():
            conn.execute(
                "DELETE FROM recorder_markers WHERE recorder = %s AND at >= %s", (recorder, hour)
            )
            conn.execute(
                "DELETE FROM bad_messages WHERE recorder = %s AND received_at >= %s",
                (recorder, hour),
            )
            conn.execute(
                """
                INSERT INTO load_progress (recorder, file_name, records, data_until, floor)
                VALUES (%s, NULL, 0, NULL, %s)
                ON CONFLICT (recorder) DO UPDATE SET
                    file_name = NULL, records = 0, data_until = NULL, floor = EXCLUDED.floor
                """,
                (recorder, floor),
            )
    return truncated
