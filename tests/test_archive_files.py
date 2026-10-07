import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ov_reliability.collector.archive import (
    apply_retention,
    file_name,
    hour_of,
    list_archive,
    scan_archive,
)


def touch(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"")
    return path


def test_hour_of_truncates_to_utc_hour() -> None:
    at = datetime(2026, 10, 7, 16, 59, 59, 999000, tzinfo=UTC)
    assert hour_of(at) == datetime(2026, 10, 7, 16, tzinfo=UTC)


def test_file_name_marks_utc() -> None:
    assert file_name(datetime(2026, 10, 7, 14, tzinfo=UTC), 0) == "2026-10-07T14Z.0.jsonl.gz"


def test_listing_sorts_by_hour_then_numeric_seq(tmp_path: Path) -> None:
    for name in [
        "2026-10-07T15Z.0.jsonl.gz",
        "2026-10-07T14Z.10.jsonl.gz",
        "2026-10-07T14Z.2.jsonl.gz",
        "2026-10-07T14Z.0.jsonl.gz",
    ]:
        touch(tmp_path, name)
    names = [f.path.name for f in list_archive(tmp_path)]
    assert names == [
        "2026-10-07T14Z.0.jsonl.gz",
        "2026-10-07T14Z.2.jsonl.gz",
        "2026-10-07T14Z.10.jsonl.gz",
        "2026-10-07T15Z.0.jsonl.gz",
    ]


def test_scan_reports_names_that_do_not_match(tmp_path: Path) -> None:
    touch(tmp_path, "2026-10-07T14Z.0.jsonl.gz")
    odd = [
        touch(tmp_path, "notes.txt"),
        touch(tmp_path, "2026-13-01T14Z.0.jsonl.gz"),
        touch(tmp_path, "2026-10-07T14.jsonl.gz"),
    ]
    (tmp_path / "subdir").mkdir()
    scan = scan_archive(tmp_path)
    assert [f.path.name for f in scan.files] == ["2026-10-07T14Z.0.jsonl.gz"]
    assert sorted(scan.unknown) == sorted([*odd, tmp_path / "subdir"])


def test_retention_boundary(tmp_path: Path) -> None:
    now = datetime(2026, 10, 7, 12, 0, 30, tzinfo=UTC)
    expired = touch(tmp_path, "2026-10-06T11Z.0.jsonl.gz")  # ends 12:00, before now - 1 day
    kept = touch(tmp_path, "2026-10-06T12Z.0.jsonl.gz")  # ends 13:00, after now - 1 day
    deleted = apply_retention(tmp_path, now, retention_days=1)
    assert deleted == [expired]
    assert not expired.exists()
    assert kept.exists()


def test_retention_leaves_unknown_files_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stray = touch(tmp_path, "2020-01-01.backup")
    with caplog.at_level(logging.WARNING):
        apply_retention(tmp_path, datetime(2026, 10, 7, tzinfo=UTC), retention_days=1)
    assert stray.exists()
    assert "2020-01-01.backup" in caplog.text
