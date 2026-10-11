"""Entry point: python -m ov_reliability.loader [replay]."""

import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from importlib.metadata import version

import psycopg

from ov_reliability.collector.archive import CorruptArchiveError, list_archive
from ov_reliability.config import LoaderConfig, load_loader_config
from ov_reliability.kv6.parse import LOCAL
from ov_reliability.loader.batch import build_batch, window_start
from ov_reliability.loader.follow import Follower
from ov_reliability.loader.partitions import drop_partitions, try_lock
from ov_reliability.loader.store import (
    MigrationError,
    migrate,
    read_progress,
    reset_for_replay,
    write_batch,
)
from ov_reliability.runtime import (
    EXIT_BAD_CONFIG,
    config_path,
    setup_logging,
    stop_on_signal,
    utc_now,
)

log = logging.getLogger("ov_reliability.loader")

EXIT_FATAL = 1
HOUR = timedelta(hours=1)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["replay"]):
        log.error("Usage: python -m ov_reliability.loader [replay]")
        return EXIT_BAD_CONFIG
    path = config_path()
    try:
        settings = load_loader_config(path)
    except (OSError, ValueError) as exc:
        log.error("Cannot use configuration %s: %s", path, exc)
        return EXIT_BAD_CONFIG
    for archive in settings.archives:
        if not archive.directory.is_dir():
            log.error("Archive directory %s does not exist", archive.directory)
            return EXIT_BAD_CONFIG
    url = os.environ.get("OV_DATABASE_URL")
    if not url:
        log.error("OV_DATABASE_URL is not set")
        return EXIT_BAD_CONFIG

    with psycopg.connect(url, autocommit=True) as conn:
        if not try_lock(conn):
            log.error("Another loader or a replay is using the database; stop it first")
            return EXIT_FATAL
        try:
            applied = migrate(conn)
        except MigrationError as exc:
            log.error("Cannot migrate the database: %s", exc)
            return EXIT_FATAL
        if applied:
            log.info("Applied migrations %s", applied)
        if args == ["replay"]:
            return replay(conn, settings)
        log.info("Started version %s", version("ov-reliability"))
        try:
            return run(conn, settings, utc_now, stop_on_signal(), time.sleep)
        except CorruptArchiveError as exc:
            log.error("Archive file is damaged: %s", exc)
            return EXIT_FATAL


def replay(conn: psycopg.Connection, settings: LoaderConfig) -> int:
    oldest: dict[str, datetime] = {}
    for archive in settings.archives:
        files = list_archive(archive.directory)
        if not files:
            log.error("Archive %s has no files to replay", archive.directory)
            return EXIT_FATAL
        oldest[archive.recorder] = files[0].hour
    # The oldest file starts mid-day, so only the days after it can be rebuilt completely.
    floor = max(hour.date() for hour in oldest.values()) + timedelta(days=1)
    emptied = reset_for_replay(conn, oldest, floor)
    log.info(
        "Emptied %d partitions from %s on; start the loader to rebuild them", len(emptied), floor
    )
    return 0


def run(
    conn: psycopg.Connection,
    settings: LoaderConfig,
    clock: Callable[[], datetime],
    should_stop: Callable[[], bool],
    sleep: Callable[[float], None],
) -> int:
    followers: dict[str, Follower] = {}
    floors: dict[str, date | None] = {}
    for archive in settings.archives:
        progress = read_progress(conn, archive.recorder)
        followers[archive.recorder] = Follower(archive.directory, progress.position)
        floors[archive.recorder] = progress.floor
        log.info("Loading %s from %s", archive.recorder, progress.position)

    counts: dict[str, Counter[str]] = {recorder: Counter() for recorder in followers}
    until: dict[str, datetime | None] = dict.fromkeys(followers)
    last_drop: datetime | None = None
    next_health = clock() + HOUR
    try:
        while not should_stop():
            busy = False
            today = clock().astimezone(LOCAL).date()
            for recorder, follower in followers.items():
                before = follower.position
                found, position = follower.poll()
                if not found and position == before:
                    continue
                batch = build_batch(
                    found, settings.data_owners, today, settings.window_days, floors[recorder]
                )
                data_until = found[-1].received_at if found else None
                write_batch(conn, recorder, batch, position, data_until)
                busy = busy or bool(found)
                until[recorder] = data_until or until[recorder]
                counts[recorder].update(
                    records=len(found),
                    events=len(batch.events),
                    mutations=len(batch.mutations),
                    bad=len(batch.bad),
                )
            now = clock()
            if last_drop is None or now - last_drop >= HOUR:
                with conn.transaction():
                    dropped = drop_partitions(conn, window_start(today, settings.window_days))
                if dropped:
                    log.info("Dropped partitions older than the window: %s", ", ".join(dropped))
                last_drop = now
            if now >= next_health:
                for recorder, count in counts.items():
                    _log_health(recorder, count, until[recorder], now)
                    count.clear()
                next_health = now + HOUR
            if not busy:
                _wait(settings.poll_seconds, should_stop, sleep)
    finally:
        for follower in followers.values():
            follower.close()
    log.info("Stopped")
    return 0


def _log_health(recorder: str, count: Counter[str], until: datetime | None, now: datetime) -> None:
    if until is None:
        behind = "nothing loaded yet"
    else:
        behind = f"{(now - until).total_seconds():.0f} s behind"
    # Bad messages never stop loading, so the hourly line is where they get noticed.
    level = logging.WARNING if count["bad"] else logging.INFO
    log.log(
        level,
        "Last hour from %s: %d records, %d stop events, %d mutations, %d bad messages; "
        "data until %s (%s)",
        recorder,
        count["records"],
        count["events"],
        count["mutations"],
        count["bad"],
        until.isoformat() if until else "-",
        behind,
    )


def _wait(seconds: float, should_stop: Callable[[], bool], sleep: Callable[[float], None]) -> None:
    # Short steps, so a stop signal doesn't wait out the whole poll interval.
    waited = 0.0
    while waited < seconds and not should_stop():
        step = min(0.5, seconds - waited)
        sleep(step)
        waited += step


if __name__ == "__main__":
    sys.exit(main())
