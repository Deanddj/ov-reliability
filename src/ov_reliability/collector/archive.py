"""Hourly gzipped JSON Lines archive: naming, listing, writing, reading and retention."""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
