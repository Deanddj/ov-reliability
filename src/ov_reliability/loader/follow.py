"""Reading archive files from a saved position onwards, including the file still being written."""

import logging
from dataclasses import dataclass
from pathlib import Path

from ov_reliability.collector.archive import (
    ArchiveFile,
    ArchiveTail,
    list_archive,
    parse_file_name,
)
from ov_reliability.collector.records import Record

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Position:
    """How far an archive has been consumed: a file and the number of its records read."""

    file_name: str | None  # None: before the oldest file
    records: int = 0


class Follower:
    def __init__(self, directory: Path, position: Position, max_records: int = 20_000) -> None:
        self._directory = directory
        self._position = position
        self._max_records = max_records
        self._tail: ArchiveTail | None = None
        self._buffer: list[Record] = []
        self._to_skip = position.records

    @property
    def position(self) -> Position:
        return self._position

    def poll(self) -> tuple[list[Record], Position]:
        """The next records (at most max_records) and the position after them."""
        records: list[Record] = []
        while len(records) < self._max_records:
            if self._buffer:
                take = self._buffer[: self._max_records - len(records)]
                del self._buffer[: len(take)]
                records.extend(take)
                self._position = Position(
                    self._position.file_name, self._position.records + len(take)
                )
                continue
            if self._tail is None and not self._open_current():
                break
            if not self._fill_buffer():
                break
        return records, self._position

    def close(self) -> None:
        if self._tail is not None:
            self._tail.close()
            self._tail = None

    def _fill_buffer(self) -> bool:
        """Read more of the current file, or move on to the next. False: nothing new yet."""
        assert self._tail is not None
        new = self._tail.read()
        if new is None:
            later = self._later_files()
            if not later:
                return False
            # The collector finishes a file before it starts the next, so with a later file
            # listed, whatever this file will ever contain is on disk now.
            while (rest := self._tail.read()) is not None:
                self._buffer_records(rest)
            if self._buffer:
                return True
            if not self._tail.finished:
                log.warning("%s is cut off; read up to the last complete line", self._name)
            if self._to_skip:
                log.error(
                    "%s ended %d records before the saved position", self._name, self._to_skip
                )
                self._to_skip = 0
            self._tail.close()
            self._tail = None
            self._position = Position(later[0].path.name, 0)
            return True
        self._buffer_records(new)
        return True

    def _buffer_records(self, records: list[Record]) -> None:
        if self._to_skip:
            skipped = min(self._to_skip, len(records))
            self._to_skip -= skipped
            records = records[skipped:]
        self._buffer.extend(records)

    def _open_current(self) -> bool:
        files = list_archive(self._directory)
        if not files:
            return False
        name = self._position.file_name
        if name is None:
            self._position = Position(files[0].path.name, 0)
        elif not any(f.path.name == name for f in files):
            later = self._later_files(files)
            log.error("Archive file %s is gone; continuing after it", name)
            if not later:
                return False
            self._position = Position(later[0].path.name, 0)
            self._to_skip = 0
        self._tail = ArchiveTail(self._directory / self._name)
        return True

    @property
    def _name(self) -> str:
        assert self._position.file_name is not None
        return self._position.file_name

    def _later_files(self, files: list[ArchiveFile] | None = None) -> list[ArchiveFile]:
        current = parse_file_name(self._name)
        if current is None:
            raise ValueError(f"not an archive file name: {self._name!r}")
        listed = list_archive(self._directory) if files is None else files
        return [f for f in listed if (f.hour, f.seq) > current]
