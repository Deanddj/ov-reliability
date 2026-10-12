"""Writing the hand-made NeTEx fixture as an archived timetable file."""

import gzip
from datetime import UTC, datetime
from pathlib import Path

from ov_reliability.timetables.store import IndexEntry

SMALL = (Path(__file__).parent / "fixtures" / "netex_small.xml").read_text(encoding="utf-8")
NAME = "NeTEx_HTM_Rail_20261001_20261005_new.xml.gz"


def write_netex(
    directory: Path, text: str = SMALL, name: str = NAME, operator: str = "htm"
) -> Path:
    path = directory / operator / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(text.encode()))
    return path


def index_line(file: str, operator: str = "htm") -> str:
    entry = IndexEntry(
        stored_at=datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
        operator=operator,
        name=file.split("/")[1],
        file=file,
        bytes=1,
        sha256="0" * 64,
        listed_modified=None,
    )
    return entry.to_json() + "\n"
