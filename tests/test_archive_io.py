import base64
import gzip
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ov_reliability.collector.archive import (
    ArchiveWriter,
    CorruptArchiveError,
    hour_of,
    list_archive,
    read_archive,
)
from ov_reliability.collector.records import Message, Record

T = datetime(2026, 10, 7, 14, 59, 59, 999000, tzinfo=UTC)


def msg(at: datetime, payload: str = "<x/>") -> Message:
    return Message(at, "/RIG/KV6posinfo", payload)


def read_all(directory: Path) -> list[Record]:
    return [record for f in list_archive(directory) for record in read_archive(f.path)]


def test_record_on_the_hour_goes_to_the_new_file(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(T))
    writer.write(msg(T + timedelta(milliseconds=1)))  # 15:00:00.000
    writer.close()
    names = [f.path.name for f in list_archive(tmp_path)]
    assert names == ["2026-10-07T14Z.0.jsonl.gz", "2026-10-07T15Z.0.jsonl.gz"]


def test_tick_in_next_hour_closes_quiet_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(T - timedelta(minutes=30)))
    writer.maintain(T + timedelta(seconds=1))
    assert writer.current_path is None
    with caplog.at_level(logging.WARNING):
        assert len(read_all(tmp_path)) == 1
    assert caplog.text == ""


def test_flush_happens_after_flush_seconds(tmp_path: Path) -> None:
    start = T - timedelta(minutes=30)
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(start))
    path = writer.current_path
    assert path is not None
    writer.maintain(start + timedelta(seconds=4))
    assert list(read_archive(path)) == []
    writer.maintain(start + timedelta(seconds=5))
    assert list(read_archive(path)) == [msg(start)]
    writer.close()


def test_flush_continues_after_clock_steps_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first = datetime(2026, 10, 7, 14, 40, 0, tzinfo=UTC)
    second = datetime(2026, 10, 7, 14, 10, 0, tzinfo=UTC)
    third = datetime(2026, 10, 7, 14, 10, 7, tzinfo=UTC)
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(first))
    writer.maintain(first + timedelta(seconds=6))
    writer.write(msg(second))
    writer.maintain(second + timedelta(seconds=1))
    writer.write(msg(third))
    writer.maintain(third)
    path = writer.current_path
    assert path is not None
    with caplog.at_level(logging.WARNING):
        assert list(read_archive(path)) == [msg(first), msg(second), msg(third)]
    writer.close()


def test_file_larger_than_one_read_chunk_reads_back(tmp_path: Path) -> None:
    start = T - timedelta(minutes=30)
    messages = [
        msg(start + timedelta(seconds=i), base64.b64encode(os.urandom(3000)).decode())
        for i in range(200)
    ]
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    for message in messages:
        writer.write(message)
    path = writer.current_path
    assert path is not None
    writer.close()

    assert path.stat().st_size > 64 * 1024
    assert list(read_archive(path)) == messages


def test_restart_in_same_hour_never_touches_the_old_file(tmp_path: Path) -> None:
    first = ArchiveWriter(tmp_path, flush_seconds=5)
    first.write(msg(T, "first"))
    first.close()
    original = (tmp_path / "2026-10-07T14Z.0.jsonl.gz").read_bytes()

    second = ArchiveWriter(tmp_path, flush_seconds=5)
    second.write(msg(T, "second"))
    second.close()

    assert (tmp_path / "2026-10-07T14Z.0.jsonl.gz").read_bytes() == original
    assert [r for r in read_all(tmp_path) if isinstance(r, Message)] == [
        msg(T, "first"),
        msg(T, "second"),
    ]


def test_clock_going_back_uses_next_free_seq(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(T - timedelta(minutes=10)))  # 14:49 → T14Z.0
    writer.write(msg(T + timedelta(minutes=10)))  # 15:09 → T15Z.0
    writer.write(msg(T - timedelta(minutes=5)))  # back to 14:54 → T14Z.1
    writer.close()
    names = [f.path.name for f in list_archive(tmp_path)]
    assert names == [
        "2026-10-07T14Z.0.jsonl.gz",
        "2026-10-07T14Z.1.jsonl.gz",
        "2026-10-07T15Z.0.jsonl.gz",
    ]


def test_cut_off_file_reads_up_to_last_flush(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    start = T - timedelta(minutes=30)
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(start, "kept"))
    writer.flush(start)
    path = writer.current_path
    assert path is not None
    flushed_size = path.stat().st_size
    writer.write(msg(start, "lost"))
    writer.flush(start)
    cut = tmp_path / "cut.jsonl.gz"
    shutil.copyfile(path, cut)
    with cut.open("r+b") as file:
        file.truncate(flushed_size + 5)
    writer.close()

    with caplog.at_level(logging.WARNING):
        assert list(read_archive(cut)) == [msg(start, "kept")]
    assert "cut.jsonl.gz" in caplog.text


def test_empty_file_reads_as_cut_off(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "2026-10-07T14Z.0.jsonl.gz"
    path.write_bytes(b"")
    with caplog.at_level(logging.WARNING):
        assert list(read_archive(path)) == []
    assert path.name in caplog.text


def test_invalid_line_in_complete_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl.gz"
    path.write_bytes(gzip.compress(b"not json\n"))
    with pytest.raises(CorruptArchiveError):
        list(read_archive(path))


def test_data_after_gzip_end_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl.gz"
    path.write_bytes(gzip.compress(b"") + b"garbage")
    with pytest.raises(CorruptArchiveError):
        list(read_archive(path))


def test_not_gzip_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl.gz"
    path.write_bytes(b"plain text, not gzip\n")
    with pytest.raises(CorruptArchiveError):
        list(read_archive(path))


def test_missing_directory_raises(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path / "missing", flush_seconds=5)
    with pytest.raises(FileNotFoundError):
        writer.write(msg(T))


timestamps = st.datetimes(
    min_value=datetime(2026, 10, 7), max_value=datetime(2026, 10, 9), timezones=st.just(UTC)
).map(lambda at: at.replace(microsecond=at.microsecond // 1000 * 1000))
messages = st.lists(
    st.builds(
        Message,
        received_at=timestamps,
        topic=st.sampled_from(["/RIG/KV6posinfo", "/GVB/KV17cvlinfo"]),
        payload=st.text(),
    ),
    max_size=40,
).map(lambda items: sorted(items, key=lambda m: m.received_at))


@settings(max_examples=50, deadline=None)
@given(messages)
def test_written_messages_read_back_identically(written: list[Message]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        writer = ArchiveWriter(directory, flush_seconds=5)
        for message in written:
            writer.write(message)
        writer.close()
        read_back = []
        for archive_file in list_archive(directory):
            for record in read_archive(archive_file.path):
                assert hour_of(record.received_at) == archive_file.hour
                read_back.append(record)
        assert read_back == written
