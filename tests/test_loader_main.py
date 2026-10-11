import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from kv6xml import Event, kv6_message
from test_store import contents

from ov_reliability.collector.archive import ArchiveWriter
from ov_reliability.collector.records import Message
from ov_reliability.config import ArchiveSource, LoaderConfig
from ov_reliability.loader.__main__ import main, run
from ov_reliability.loader.batch import build_batch
from ov_reliability.loader.follow import Position
from ov_reliability.loader.partitions import try_lock
from ov_reliability.loader.store import read_progress, write_batch

CONFIG = """
[loader]
data_owners = ["HTM"]
window_days = 90
poll_seconds = 5

[[loader.archives]]
recorder = "collector"
directory = "{directory}"
"""
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def settings(directory: Path) -> LoaderConfig:
    return LoaderConfig(
        data_owners=("HTM",),
        window_days=90,
        poll_seconds=5,
        archives=(ArchiveSource(recorder="collector", directory=directory),),
    )


def archive(directory: Path, days: list[str]) -> None:
    """One message per day at 08:00 UTC, for the given operating days."""
    writer = ArchiveWriter(directory, flush_seconds=5)
    for day in days:
        at = datetime.fromisoformat(f"{day}T08:00:00+00:00")
        message = kv6_message([Event(day=day, timestamp=at.isoformat())])
        writer.write(Message(at, "/RIG/KV6posinfo", message))
    writer.close()


def stop_after(loops: int) -> Callable[[], bool]:
    calls = 0

    def should_stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > loops

    return should_stop


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, directory: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(directory=directory), encoding="utf-8")
    monkeypatch.setenv("OV_CONFIG", str(config))


def test_missing_config_exits_with_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OV_CONFIG", str(tmp_path / "missing.toml"))
    assert main([]) == 2
    assert "missing.toml" in caplog.text


def test_missing_archive_directory_exits_with_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configure(monkeypatch, tmp_path, tmp_path / "missing")
    assert main([]) == 2
    assert "does not exist" in caplog.text


def test_missing_database_url_exits_with_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configure(monkeypatch, tmp_path, tmp_path)
    monkeypatch.delenv("OV_DATABASE_URL", raising=False)
    assert main([]) == 2
    assert "OV_DATABASE_URL" in caplog.text


def test_unknown_argument_exits_with_2() -> None:
    assert main(["replay", "now"]) == 2


def test_run_loads_the_archive_and_logs_health(
    database: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    archive(tmp_path, ["2026-10-08", "2026-10-09"])
    times = iter([NOW + timedelta(minutes=minutes) for minutes in (0, 0, 1, 2, 70, 71, 72)])
    slept: list[float] = []
    with caplog.at_level(logging.INFO):
        assert (
            run(database, settings(tmp_path), lambda: next(times), stop_after(3), slept.append) == 0
        )
    [(count,)] = database.execute("SELECT count(*) FROM stop_events").fetchall()
    assert count == 2
    progress = read_progress(database, "collector")
    assert progress.position == Position("2026-10-09T08Z.0.jsonl.gz", 1)
    assert progress.data_until == datetime(2026, 10, 9, 8, tzinfo=UTC)
    assert "Last hour from collector: 2 records, 2 stop events" in caplog.text
    assert slept  # waited once it had caught up


def test_replay_keeps_days_before_floor(
    database: psycopg.Connection, schema_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive(tmp_path, ["2026-10-07", "2026-10-08", "2026-10-09"])
    # A day whose raw files are already gone.
    event = Event(day="2026-10-05", timestamp="2026-10-05T10:00:00+02:00")
    old = build_batch(
        [Message(NOW, "/RIG/KV6posinfo", kv6_message([event]))], ["HTM"], NOW.date(), 90, None
    )
    write_batch(database, "collector", old, Position(None), None)
    run(database, settings(tmp_path), lambda: NOW, stop_after(2), lambda _: None)
    before = contents(database)

    configure(monkeypatch, tmp_path, tmp_path)
    monkeypatch.setenv("OV_DATABASE_URL", schema_url)
    assert main(["replay"]) == 0
    assert read_progress(database, "collector").floor == date(2026, 10, 8)
    [(count,)] = database.execute("SELECT count(*) FROM stop_events").fetchall()
    assert count == 2  # 10-05 and 10-07, the days before the floor

    run(database, settings(tmp_path), lambda: NOW, stop_after(2), lambda _: None)
    assert contents(database)["stop_events"] == before["stop_events"]
    assert len(before["stop_events"]) == 4


def test_replay_refuses_while_the_loader_runs(
    database: psycopg.Connection,
    schema_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    archive(tmp_path, ["2026-10-09"])
    configure(monkeypatch, tmp_path, tmp_path)
    monkeypatch.setenv("OV_DATABASE_URL", schema_url)
    assert try_lock(database)
    assert main(["replay"]) == 1
    assert "Another loader" in caplog.text
    database.execute("SELECT pg_advisory_unlock_all()")


def test_health_line_is_a_warning_with_bad_messages(
    database: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(Message(NOW - timedelta(hours=1), "/RIG/KV6posinfo", "<broken>HTM<"))
    writer.close()
    times = iter([NOW, NOW, NOW + timedelta(minutes=61), NOW + timedelta(minutes=61)])
    with caplog.at_level(logging.INFO):
        run(database, settings(tmp_path), lambda: next(times), stop_after(1), lambda _: None)
    [record] = [r for r in caplog.records if "Last hour" in r.message]
    assert record.levelno == logging.WARNING
    assert "1 bad messages" in record.message
