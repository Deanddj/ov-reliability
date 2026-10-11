"""Hourly gzipped JSON Lines archive: naming, listing, writing, reading and retention."""

import gzip
import logging
import os
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BufferedWriter
from pathlib import Path
from typing import Self

from ov_reliability.collector.records import InvalidRecordError, Record, decode, encode

log = logging.getLogger(__name__)

_CHUNK_BYTES = 64 * 1024
_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2})Z\.(\d+)\.jsonl\.gz$")


def hour_of(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def file_name(hour: datetime, seq: int) -> str:
    return f"{hour:%Y-%m-%dT%H}Z.{seq}.jsonl.gz"


@dataclass(frozen=True, order=True)
class ArchiveFile:
    hour: datetime
    seq: int
    path: Path = field(compare=False)


@dataclass(frozen=True)
class ArchiveScan:
    files: list[ArchiveFile]
    unknown: list[Path]


def scan_archive(directory: Path) -> ArchiveScan:
    files: list[ArchiveFile] = []
    unknown: list[Path] = []
    for path in directory.iterdir():
        parsed = _parse_name(path)
        if parsed is None:
            unknown.append(path)
        else:
            files.append(parsed)
    return ArchiveScan(sorted(files), sorted(unknown))


def list_archive(directory: Path) -> list[ArchiveFile]:
    return scan_archive(directory).files


def apply_retention(directory: Path, now: datetime, retention_days: int) -> list[Path]:
    cutoff = now - timedelta(days=retention_days)
    scan = scan_archive(directory)
    for path in scan.unknown:
        log.warning("Leaving unexpected entry in archive directory alone: %s", path.name)
    deleted: list[Path] = []
    for archive_file in scan.files:
        if archive_file.hour + timedelta(hours=1) <= cutoff:
            archive_file.path.unlink()
            log.info("Deleted %s (older than %d days)", archive_file.path.name, retention_days)
            deleted.append(archive_file.path)
    return deleted


def parse_file_name(name: str) -> tuple[datetime, int] | None:
    """The (hour, seq) an archive file name stands for, or None if it isn't one."""
    match = _NAME.match(name)
    if match is None:
        return None
    try:
        hour = datetime.strptime(match[1], "%Y-%m-%dT%H").replace(tzinfo=UTC)
    except ValueError:
        return None
    return hour, int(match[2])


def _parse_name(path: Path) -> ArchiveFile | None:
    parsed = parse_file_name(path.name)
    if parsed is None or not path.is_file():
        return None
    return ArchiveFile(*parsed, path)


class CorruptArchiveError(Exception):
    pass


class ArchiveWriter:
    def __init__(self, directory: Path, flush_seconds: float) -> None:
        self._directory = directory
        self._flush_interval = timedelta(seconds=flush_seconds)
        self._raw: BufferedWriter | None = None
        self._gzip: gzip.GzipFile | None = None
        self._path: Path | None = None
        self._hour: datetime | None = None
        self._last_flush: datetime | None = None
        self._dirty = False

    @property
    def current_path(self) -> Path | None:
        return self._path

    def write(self, record: Record) -> int:
        hour = hour_of(record.received_at)
        if self._gzip is None or hour != self._hour:
            self._close_file()
            self._open_file(hour, record.received_at)
        assert self._gzip is not None
        data = (encode(record) + "\n").encode("utf-8")
        self._gzip.write(data)
        self._dirty = True
        return len(data)

    def maintain(self, now: datetime) -> None:
        if self._gzip is None:
            return
        if hour_of(now) != self._hour:
            self._close_file()
        elif self._last_flush is not None:
            since = now - self._last_flush
            # A clock that stepped back would otherwise postpone the next flush by the step size.
            if since >= self._flush_interval or since < timedelta(0):
                self.flush(now)

    def flush(self, now: datetime) -> None:
        if self._gzip is not None and self._dirty:
            # A sync flush ends on a byte boundary, so a reader can decompress everything up
            # to here even if the file is cut off later.
            self._gzip.flush(zlib.Z_SYNC_FLUSH)
            self._dirty = False
        self._last_flush = now

    def close(self) -> None:
        self._close_file()

    def _open_file(self, hour: datetime, now: datetime) -> None:
        seq = 1 + max((f.seq for f in list_archive(self._directory) if f.hour == hour), default=-1)
        while True:
            path = self._directory / file_name(hour, seq)
            try:
                raw = path.open("xb")
            except FileExistsError:
                seq += 1
                continue
            break
        self._raw = raw
        self._gzip = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
        self._path = path
        self._hour = hour
        self._last_flush = now
        self._dirty = False

    def _close_file(self) -> None:
        if self._gzip is None or self._raw is None:
            return
        self._gzip.close()
        self._raw.flush()
        os.fsync(self._raw.fileno())
        self._raw.close()
        self._gzip = None
        self._raw = None
        self._path = None
        self._hour = None


class ArchiveTail:
    """Reads an archive file a chunk at a time, including bytes appended after it was opened."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file = path.open("rb")
        self._decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
        self._pending = b""

    @property
    def finished(self) -> bool:
        """The gzip stream has ended, so the file will not grow any more."""
        return self._decompressor.eof

    @property
    def partial_line(self) -> bool:
        return bool(self._pending)

    def read(self) -> list[Record] | None:
        """The records completed by the next chunk, or None if there are no new bytes yet."""
        chunk = self._file.read(_CHUNK_BYTES)
        if not chunk:
            return None
        if self._decompressor.eof:
            raise CorruptArchiveError(f"{self._path.name}: data after the end of the gzip stream")
        try:
            self._pending += self._decompressor.decompress(chunk)
        except zlib.error as exc:
            raise CorruptArchiveError(f"{self._path.name}: {exc}") from None
        if self._decompressor.unused_data:
            raise CorruptArchiveError(f"{self._path.name}: data after the end of the gzip stream")
        *lines, self._pending = self._pending.split(b"\n")
        return [_decode_line(self._path, line) for line in lines]

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_archive(path: Path) -> Iterator[Record]:
    with ArchiveTail(path) as tail:
        while (records := tail.read()) is not None:
            yield from records
        if not tail.finished:
            log.warning("%s is cut off; read up to the last complete line", path.name)
        elif tail.partial_line:
            log.warning("%s ends without a newline; ignored the last partial line", path.name)


def _decode_line(path: Path, line: bytes) -> Record:
    try:
        return decode(line.decode("utf-8"))
    except (UnicodeDecodeError, InvalidRecordError) as exc:
        raise CorruptArchiveError(f"{path.name}: {exc}") from None
