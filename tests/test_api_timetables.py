from datetime import date
from pathlib import Path

import pytest
from netexfile import NAME, SMALL, index_line, write_netex

from ov_reliability.api import timetables
from ov_reliability.api.timetables import PlanCache
from ov_reliability.timetables.netex import TimetableFile, read_timetable
from ov_reliability.timetables.plan import NoTimetableError

SUNDAY = date(2026, 10, 11)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    found: list[str] = []

    def counting(path: Path) -> TimetableFile:
        found.append(path.name)
        return read_timetable(path)

    monkeypatch.setattr(timetables, "read_timetable", counting)
    return found


def test_a_file_is_parsed_once(tmp_path: Path, reads: list[str]) -> None:
    write_netex(tmp_path)
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{NAME}"))
    cache = PlanCache(tmp_path)
    first = cache.timetables("htm", SUNDAY)
    second = cache.timetables("htm", date(2026, 10, 10))
    assert [f.header.file_name for f in first] == [NAME]
    assert second == first
    assert reads == [NAME]


def test_least_recently_used_file_is_dropped(tmp_path: Path, reads: list[str]) -> None:
    other = "NeTEx_HTM_Bus_20261001_20261005_new.xml.gz"
    write_netex(tmp_path)
    write_netex(tmp_path, SMALL, other)
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{NAME}") + index_line(f"htm/{other}"))
    cache = PlanCache(tmp_path, max_files=1)
    cache.timetables("htm", SUNDAY)
    cache.timetables("htm", SUNDAY)
    assert reads == [other, NAME, other, NAME]


def test_no_timetable_passes_through(tmp_path: Path) -> None:
    write_netex(tmp_path)
    (tmp_path / "index.jsonl").write_text(index_line(f"htm/{NAME}"))
    with pytest.raises(NoTimetableError, match="htm covers 2026-10-12"):
        PlanCache(tmp_path).timetables("htm", date(2026, 10, 12))
