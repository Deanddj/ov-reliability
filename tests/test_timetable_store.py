import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ov_reliability.timetables.listing import Entry
from ov_reliability.timetables.store import (
    INDEX_NAME,
    ArchiveMismatchError,
    BadDownloadError,
    StoreOutcome,
    TimetableStore,
    check_download,
)

NOW = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
NAME = "NeTEx_RET_ALL_20261005_20261005_version415_v930.xml.gz"


def netex(body: str = "a", bom: bool = False) -> bytes:
    xml = f'<?xml version="1.0" encoding="utf-8"?><PublicationDelivery>{body}</PublicationDelivery>'
    return gzip.compress((b"\xef\xbb\xbf" if bom else b"") + xml.encode(), mtime=0)


def open_store(directory: Path) -> TimetableStore:
    store = TimetableStore(directory, lambda: NOW)
    store.open()
    return store


def index_lines(directory: Path) -> list[dict[str, object]]:
    text = (directory / INDEX_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_new_file_is_stored_with_one_index_line(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    data = netex()
    entry = Entry(NAME, "05-Oct-2026 14:11")
    assert store.needs_download("ret", entry)
    assert store.store("ret", entry, data) == StoreOutcome.STORED
    assert (tmp_path / "ret" / NAME).read_bytes() == data
    assert index_lines(tmp_path) == [
        {
            "stored_at": "2026-10-08T14:00:00.000Z",
            "operator": "ret",
            "name": NAME,
            "file": f"ret/{NAME}",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "listed_modified": "05-Oct-2026 14:11",
        }
    ]
    assert not store.needs_download("ret", entry)


def test_same_content_listed_again_is_unchanged(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex())
    moved = Entry(NAME, "06-Oct-2026 09:00")
    assert store.needs_download("ret", moved)
    assert store.store("ret", moved, netex()) == StoreOutcome.UNCHANGED
    assert len(index_lines(tmp_path)) == 1
    assert not store.needs_download("ret", moved)


def test_republish_is_stored_beside_the_original(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    original, newer = netex("old"), netex("new")
    store.store("ret", Entry(NAME, "05-Oct-2026 14:11"), original)
    outcome = store.store("ret", Entry(NAME, "06-Oct-2026 09:00"), newer)
    assert outcome == StoreOutcome.REPUBLISHED
    suffixed = NAME.removesuffix(".xml.gz") + f".{hashlib.sha256(newer).hexdigest()[:8]}.xml.gz"
    assert (tmp_path / "ret" / NAME).read_bytes() == original
    assert (tmp_path / "ret" / suffixed).read_bytes() == newer
    assert [line["name"] for line in index_lines(tmp_path)] == [NAME, NAME]


@pytest.mark.parametrize(
    "data",
    [
        netex()[:-10],
        b"not gzip at all",
        gzip.compress(b"PK\x03\x04 a zip, not xml", mtime=0),
        netex() + b"trailing",
        netex() + netex(),
        b"",
    ],
)
def test_bad_download_leaves_nothing(tmp_path: Path, data: bytes) -> None:
    store = open_store(tmp_path)
    with pytest.raises(BadDownloadError):
        store.store("ret", Entry(NAME, "05-Oct-2026 14:11"), data)
    assert not (tmp_path / "ret" / NAME).exists()
    assert not list((tmp_path).glob("ret/.*.part"))
    assert not (tmp_path / INDEX_NAME).exists()
    assert store.needs_download("ret", Entry(NAME, "05-Oct-2026 14:11"))


def test_accepts_bom() -> None:
    check_download(netex(bom=True))


def test_large_file_is_checked_in_full() -> None:
    body = "".join(f"<J n='{i}'/>" for i in range(300_000))
    check_download(netex(body))
    with pytest.raises(BadDownloadError):
        check_download(netex(body)[:-100])


def test_leftover_part_file_is_removed(tmp_path: Path) -> None:
    (tmp_path / "ret").mkdir()
    partial = tmp_path / "ret" / f".{NAME}.part"
    partial.write_bytes(b"half")
    open_store(tmp_path)
    assert not partial.exists()


def test_reopened_store_knows_what_is_stored(tmp_path: Path) -> None:
    entry = Entry(NAME, "05-Oct-2026 14:11")
    open_store(tmp_path).store("ret", entry, netex())
    store = open_store(tmp_path)
    assert not store.needs_download("ret", entry)
    assert store.needs_download("ret", Entry(NAME, "06-Oct-2026 09:00"))


def test_index_line_without_file_is_fatal(tmp_path: Path) -> None:
    open_store(tmp_path).store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex())
    (tmp_path / "ret" / NAME).unlink()
    with pytest.raises(ArchiveMismatchError, match="missing"):
        open_store(tmp_path)


def test_file_of_wrong_size_is_fatal(tmp_path: Path) -> None:
    open_store(tmp_path).store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex())
    (tmp_path / "ret" / NAME).write_bytes(netex("longer content"))
    with pytest.raises(ArchiveMismatchError, match="bytes"):
        open_store(tmp_path)


def test_damaged_complete_index_line_is_fatal(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex())
    with (tmp_path / INDEX_NAME).open("a", encoding="utf-8") as file:
        file.write('{"not": "an entry"}\n')
    with pytest.raises(ArchiveMismatchError, match="line 2"):
        open_store(tmp_path)


def test_drops_cut_off_last_index_line(tmp_path: Path) -> None:
    open_store(tmp_path).store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex())
    with (tmp_path / INDEX_NAME).open("a", encoding="utf-8") as file:
        file.write('{"stored_at": "2026-10-08T15:0')
    store = open_store(tmp_path)
    assert len(store.entries) == 1
    assert (tmp_path / INDEX_NAME).read_text(encoding="utf-8").endswith("}\n")


def test_adopts_unindexed_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "htm").mkdir()
    data = netex()
    (tmp_path / "htm" / "NeTEx_HTM_Bus_20260924_20261005_new.xml.gz").write_bytes(data)
    store = open_store(tmp_path)
    [entry] = store.entries
    assert entry.operator == "htm"
    assert entry.name == "NeTEx_HTM_Bus_20260924_20261005_new.xml.gz"
    assert entry.listed_modified is None
    assert entry.sha256 == hashlib.sha256(data).hexdigest()
    assert "never indexed" in caplog.text
    assert len(index_lines(tmp_path)) == 1


def test_adopted_republish_keeps_its_original_name(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex("old"))
    store.store("ret", Entry(NAME, "06-Oct-2026 09:00"), netex("new"))
    lines = (tmp_path / INDEX_NAME).read_text(encoding="utf-8").splitlines(keepends=True)
    (tmp_path / INDEX_NAME).write_text(lines[0], encoding="utf-8")
    reopened = open_store(tmp_path)
    assert [entry.name for entry in reopened.entries] == [NAME, NAME]


def test_unexpected_entries_are_left_alone(tmp_path: Path) -> None:
    (tmp_path / "ret").mkdir()
    (tmp_path / "ret" / "notes.txt").write_text("hello", encoding="utf-8")
    store = open_store(tmp_path)
    assert store.entries == []
    assert (tmp_path / "ret" / "notes.txt").exists()


NAMES = st.sampled_from(["NeTEx_A_1.xml.gz", "NeTEx_A_2.xml.gz", "NeTEx_B_1.xml.gz"])
LISTING = st.lists(
    st.tuples(NAMES, st.sampled_from(["t1", "t2", "t3"]), st.integers(0, 2)),
    max_size=4,
)


@settings(max_examples=60, deadline=None)
@given(checks=st.lists(st.tuples(LISTING, st.booleans()), max_size=6))
def test_archive_holds_every_version_once_and_never_changes(
    tmp_path_factory: pytest.TempPathFactory,
    checks: list[tuple[list[tuple[str, str, int]], bool]],
) -> None:
    directory = tmp_path_factory.mktemp("store")
    store = open_store(directory)
    served: set[tuple[str, bytes]] = set()
    seen_files: dict[Path, bytes] = {}
    for listing, reopen in checks:
        if reopen:
            store = open_store(directory)
        for name, modified, content in listing:
            entry = Entry(name, modified)
            data = netex(f"{name}-{content}")
            if store.needs_download("op", entry):
                store.store("op", entry, data)
                served.add((name, data))
            for path in (directory / "op").glob("*"):
                seen_files.setdefault(path, path.read_bytes())

    entries = open_store(directory).entries
    stored = {(entry.name, (directory / entry.file).read_bytes()) for entry in entries}
    assert len(stored) == len(entries)
    assert stored == served
    assert {directory / entry.file for entry in entries} == set((directory / "op").glob("*"))
    for path, data in seen_files.items():
        assert path.read_bytes() == data


def test_rebuilt_index_matches_republished_copy_to_its_original(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex("old"))
    store.store("ret", Entry(NAME, "06-Oct-2026 09:00"), netex("new"))
    (tmp_path / INDEX_NAME).unlink()
    rebuilt = open_store(tmp_path)
    assert [entry.name for entry in rebuilt.entries] == [NAME, NAME]
    # A third version must be stored without colliding with the adopted copies.
    third = Entry(NAME, "07-Oct-2026 09:00")
    assert rebuilt.needs_download("ret", third)
    assert rebuilt.store("ret", third, netex("third")) == StoreOutcome.REPUBLISHED


def test_unfinished_index_rewrite_is_removed(tmp_path: Path) -> None:
    open_store(tmp_path).store("ret", Entry(NAME, "05-Oct-2026 14:11"), netex())
    (tmp_path / f".{INDEX_NAME}.tmp").write_text("half", encoding="utf-8")
    store = open_store(tmp_path)
    assert len(store.entries) == 1
    assert not (tmp_path / f".{INDEX_NAME}.tmp").exists()


@pytest.mark.parametrize(
    ("operator", "file"),
    [
        ("ret", f"../{NAME}"),
        ("ret", f"htm/{NAME}"),
        ("../ret", f"../ret/{NAME}"),
        ("ret", f"ret/sub/{NAME}"),
        ("ret", "ret/notes.txt"),
    ],
)
def test_index_line_with_unsafe_file_is_fatal(tmp_path: Path, operator: str, file: str) -> None:
    line = {
        "stored_at": "2026-10-08T14:00:00.000Z",
        "operator": operator,
        "name": NAME,
        "file": file,
        "bytes": 1,
        "sha256": "0" * 64,
        "listed_modified": None,
    }
    (tmp_path / INDEX_NAME).write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(ArchiveMismatchError, match="line 1"):
        open_store(tmp_path)
