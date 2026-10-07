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

from ov_reliability.collector.records import InvalidRecordError, Record, decode, encode

log = logging.getLogger(__name__)

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


def _parse_name(path: Path) -> ArchiveFile | None:
    match = _NAME.match(path.name)
    if match is None or not path.is_file():
        return None
    try:
        hour = datetime.strptime(match[1], "%Y-%m-%dT%H").replace(tzinfo=UTC)
    except ValueError:
        return None
    return ArchiveFile(hour, int(match[2]), path)


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


def read_archive(path: Path) -> Iterator[Record]:
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    pending = b""
    with path.open("rb") as file:
        while chunk := file.read(64 * 1024):
            try:
                pending += decompressor.decompress(chunk)
            except zlib.error as exc:
                raise CorruptArchiveError(f"{path.name}: {exc}") from None
            *lines, pending = pending.split(b"\n")
            for line in lines:
                yield _decode_line(path, line)
            if decompressor.eof:
                if decompressor.unused_data or file.read(1):
                    raise CorruptArchiveError(f"{path.name}: data after the end of the gzip stream")
                break
    if not decompressor.eof:
        log.warning("%s is cut off; read up to the last complete line", path.name)
    elif pending:
        log.warning("%s ends without a newline; ignored the last partial line", path.name)


def _decode_line(path: Path, line: bytes) -> Record:
    try:
        return decode(line.decode("utf-8"))
    except (UnicodeDecodeError, InvalidRecordError) as exc:
        raise CorruptArchiveError(f"{path.name}: {exc}") from None
