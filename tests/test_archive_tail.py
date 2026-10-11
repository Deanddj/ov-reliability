import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from ov_reliability.collector.archive import (
    ArchiveTail,
    ArchiveWriter,
    parse_file_name,
    read_archive,
)
from ov_reliability.collector.records import Message, Record

T = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)


def msg(seconds: int, payload: str) -> Message:
    return Message(T + timedelta(seconds=seconds), "/RIG/KV6posinfo", payload)


def read_until_quiet(tail: ArchiveTail) -> list[Record]:
    records: list[Record] = []
    while (new := tail.read()) is not None:
        records.extend(new)
    return records


def test_no_new_bytes_returns_none(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(0, "a"))
    writer.flush(T)
    path = writer.current_path
    assert path is not None
    with ArchiveTail(path) as tail:
        assert read_until_quiet(tail) == [msg(0, "a")]
        assert tail.read() is None
        writer.write(msg(1, "b"))
        writer.flush(T)
        assert read_until_quiet(tail) == [msg(1, "b")]
    writer.close()


def test_finished_only_after_the_writer_closes(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, flush_seconds=5)
    writer.write(msg(0, "a"))
    writer.flush(T)
    path = writer.current_path
    assert path is not None
    with ArchiveTail(path) as tail:
        read_until_quiet(tail)
        assert not tail.finished
        writer.close()
        assert read_until_quiet(tail) == []
        assert tail.finished


@settings(max_examples=50, deadline=None)
@given(
    payloads=st.lists(st.text(max_size=300), min_size=1, max_size=30),
    flushes=st.sets(st.integers(0, 29)),
    cuts=st.lists(st.floats(0, 1), max_size=10),
)
def test_split_file_reads_like_whole(
    payloads: list[str], flushes: set[int], cuts: list[float]
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        writer = ArchiveWriter(directory, flush_seconds=5)
        for i, payload in enumerate(payloads):
            writer.write(msg(i, payload))
            if i in flushes:
                writer.flush(T)
        path = writer.current_path
        assert path is not None
        writer.close()
        whole = path.read_bytes()

        growing = directory / "growing.jsonl.gz"
        growing.write_bytes(b"")
        records: list[Record] = []
        with ArchiveTail(growing) as tail:
            for end in [*sorted(round(cut * len(whole)) for cut in cuts), len(whole)]:
                size = growing.stat().st_size
                if end > size:
                    with growing.open("ab") as file:
                        file.write(whole[size:end])
                records.extend(read_until_quiet(tail))
            assert tail.finished
        assert records == list(read_archive(path))


def test_parse_file_name() -> None:
    assert parse_file_name("2026-10-07T14Z.10.jsonl.gz") == (T, 10)
    assert parse_file_name("2026-10-07T14Z.jsonl.gz") is None
    assert parse_file_name("2026-13-07T14Z.0.jsonl.gz") is None
