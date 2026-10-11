import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ov_reliability.collector.archive import ArchiveWriter, list_archive, read_archive
from ov_reliability.collector.records import Message, Record
from ov_reliability.loader.follow import Follower, Position

T = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)


def msg(minute: int, payload: str = "<x/>") -> Message:
    return Message(T + timedelta(minutes=minute), "/RIG/KV6posinfo", payload)


def write(directory: Path, messages: list[Message]) -> None:
    writer = ArchiveWriter(directory, flush_seconds=5)
    for message in messages:
        writer.write(message)
    writer.close()


def drain(follower: Follower) -> list[Record]:
    records: list[Record] = []
    while new := follower.poll()[0]:
        records.extend(new)
    return records


def test_reads_every_file_in_order(tmp_path: Path) -> None:
    messages = [msg(minute) for minute in range(0, 180, 20)]
    write(tmp_path, messages)
    follower = Follower(tmp_path, Position(None))
    assert drain(follower) == messages
    assert follower.position == Position("2026-10-09T10Z.0.jsonl.gz", 3)


def test_restart_from_position_skips_what_was_read(tmp_path: Path) -> None:
    messages = [msg(minute) for minute in range(0, 120, 10)]
    write(tmp_path, messages)
    first = Follower(tmp_path, Position(None), max_records=4)
    records, position = first.poll()
    first.close()
    assert records == messages[:4]
    assert drain(Follower(tmp_path, position)) == messages[4:]


def test_max_records_limits_a_poll(tmp_path: Path) -> None:
    write(tmp_path, [msg(minute) for minute in range(10)])
    records, position = Follower(tmp_path, Position(None), max_records=3).poll()
    assert len(records) == 3
    assert position == Position("2026-10-09T08Z.0.jsonl.gz", 3)


def test_live_file_is_followed(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    follower = Follower(tmp_path, Position(None))
    assert follower.poll()[0] == []
    writer.write(msg(1))
    writer.flush(T)
    assert follower.poll()[0] == [msg(1)]
    assert follower.poll()[0] == []
    writer.write(msg(2))
    writer.write(msg(61))  # the next hour: closes the first file
    writer.close()
    assert drain(follower) == [msg(2), msg(61)]


def test_cut_off_file_is_left_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(tmp_path, [msg(1), msg(2)])
    first = tmp_path / "2026-10-09T08Z.0.jsonl.gz"
    data = first.read_bytes()
    first.write_bytes(data[: len(data) - 8])  # without the gzip trailer
    write(tmp_path, [msg(61)])
    with caplog.at_level(logging.WARNING):
        assert drain(Follower(tmp_path, Position(None))) == [msg(1), msg(2), msg(61)]
    assert "cut off" in caplog.text


def test_missing_position_file_continues_after_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(tmp_path, [msg(61), msg(121)])
    position = Position("2026-10-09T08Z.0.jsonl.gz", 5)
    with caplog.at_level(logging.ERROR):
        assert drain(Follower(tmp_path, position)) == [msg(61), msg(121)]
    assert "is gone" in caplog.text


def test_empty_archive_gives_nothing(tmp_path: Path) -> None:
    assert Follower(tmp_path, Position(None)).poll() == ([], Position(None))


@settings(max_examples=40, deadline=None)
@given(
    minutes=st.lists(st.integers(0, 200), min_size=1, max_size=40).map(sorted),
    steps=st.lists(st.integers(1, 2000), min_size=1, max_size=12),
    max_records=st.integers(1, 7),
    restart_after=st.integers(0, 20),
)
def test_split_file_reads_like_whole(
    minutes: list[int], steps: list[int], max_records: int, restart_after: int
) -> None:
    """Files appear and grow in random steps; a poll after each step, and one restart."""
    messages = [msg(minute, f"<x n='{i}'/>") for i, minute in enumerate(minutes)]
    with tempfile.TemporaryDirectory() as tmp:
        source, live = Path(tmp) / "source", Path(tmp) / "live"
        source.mkdir()
        live.mkdir()
        write(source, messages)
        whole = [(f.path.name, f.path.read_bytes()) for f in list_archive(source)]
        expected = [r for f in list_archive(source) for r in read_archive(f.path)]

        stream = [(name, i) for name, data in whole for i in range(len(data))]
        follower = Follower(live, Position(None), max_records)
        records: list[Record] = []
        done, polls = 0, 0
        for step in [*steps, len(stream)]:
            for name, i in stream[done : done + step]:
                with (live / name).open("ab") as file:
                    file.write(whole[[n for n, _ in whole].index(name)][1][i : i + 1])
            done = min(done + step, len(stream))
            while new := follower.poll()[0]:
                records.extend(new)
                polls += 1
                if polls == restart_after:
                    follower.close()
                    follower = Follower(live, follower.position, max_records)
        assert records == expected
