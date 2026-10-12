"""Writing the hand-made NeTEx fixture as an archived timetable file."""

import gzip
from pathlib import Path

SMALL = (Path(__file__).parent / "fixtures" / "netex_small.xml").read_text(encoding="utf-8")
NAME = "NeTEx_HTM_Rail_20261001_20261005_new.xml.gz"


def write_netex(
    directory: Path, text: str = SMALL, name: str = NAME, operator: str = "htm"
) -> Path:
    path = directory / operator / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(text.encode()))
    return path
