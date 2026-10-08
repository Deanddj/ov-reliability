"""The timetable archive on disk: one directory per operator plus an append-only index."""

import enum
import hashlib
import json
import logging
import os
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from ov_reliability.config import OPERATOR
from ov_reliability.timestamps import format_timestamp, parse_timestamp
from ov_reliability.timetables.listing import NETEX_NAME, Entry

log = logging.getLogger(__name__)

INDEX_NAME = "index.jsonl"
_SUFFIX = ".xml.gz"
_BOM = b"\xef\xbb\xbf"
_CHUNK = 64 * 1024
_REPUBLISHED = re.compile(r"^(?P<original>.+)\.[0-9a-f]{8}\.xml\.gz$")


class BadDownloadError(Exception):
    pass


class ArchiveMismatchError(Exception):
    pass


class StoreOutcome(enum.Enum):
    STORED = "stored"
    REPUBLISHED = "republished"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class IndexEntry:
    stored_at: datetime
    operator: str
    name: str
    file: str
    bytes: int
    sha256: str
    listed_modified: str | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "stored_at": format_timestamp(self.stored_at),
                "operator": self.operator,
                "name": self.name,
                "file": self.file,
                "bytes": self.bytes,
                "sha256": self.sha256,
                "listed_modified": self.listed_modified,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> Self:
        data: Any = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("index line is not an object")
        modified = data["listed_modified"]
        entry = cls(
            stored_at=parse_timestamp(_str(data, "stored_at")),
            operator=_str(data, "operator"),
            name=_str(data, "name"),
            file=_str(data, "file"),
            bytes=_int(data, "bytes"),
            sha256=_str(data, "sha256"),
            listed_modified=None if modified is None else _str(data, "listed_modified"),
        )
        operator_dir, _, file_name = entry.file.partition("/")
        if (
            not OPERATOR.match(entry.operator)
            or not NETEX_NAME.match(entry.name)
            or operator_dir != entry.operator
            or not NETEX_NAME.match(file_name)
        ):
            raise ValueError(f"unexpected operator, name or file: {entry.file!r}")
        return entry


def check_download(data: bytes) -> None:
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    head = b""
    try:
        for start in range(0, len(data), _CHUNK):
            if decompressor.eof:
                raise BadDownloadError("data after the end of the gzip stream")
            # Decompressing the whole file checks its CRC. Only the start is kept, because an
            # uncompressed timetable can be over 250 MB.
            out = decompressor.decompress(data[start : start + _CHUNK])
            if len(head) < 64:
                head += out[:64]
    except zlib.error as exc:
        raise BadDownloadError(f"not a valid gzip file: {exc}") from None
    if not decompressor.eof:
        raise BadDownloadError("gzip data ends early")
    if decompressor.unused_data:
        raise BadDownloadError("data after the end of the gzip stream")
    if not head.removeprefix(_BOM).startswith(b"<?xml"):
        raise BadDownloadError("content does not start with an XML declaration")


class TimetableStore:
    def __init__(self, directory: Path, clock: Callable[[], datetime]) -> None:
        self._directory = directory
        self._clock = clock
        self._index_path = directory / INDEX_NAME
        self._entries: list[IndexEntry] = []
        self._last_modified: dict[tuple[str, str], str | None] = {}

    @property
    def entries(self) -> list[IndexEntry]:
        return list(self._entries)

    def open(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        self._remove_partial_downloads()
        self._entries = self._read_index()
        self._check_files()
        self._adopt_unindexed_files()
        self._last_modified = {}
        for entry in self._entries:
            self._last_modified[(entry.operator, entry.name)] = entry.listed_modified

    def needs_download(self, operator: str, entry: Entry) -> bool:
        key = (operator, entry.name)
        return key not in self._last_modified or self._last_modified[key] != entry.modified

    def store(self, operator: str, entry: Entry, data: bytes) -> StoreOutcome:
        check_download(data)
        sha256 = hashlib.sha256(data).hexdigest()
        versions = [e for e in self._entries if e.operator == operator and e.name == entry.name]
        self._last_modified[(operator, entry.name)] = entry.modified
        if any(version.sha256 == sha256 for version in versions):
            log.info("%s/%s was listed again with the same content", operator, entry.name)
            return StoreOutcome.UNCHANGED

        file_name = entry.name
        if versions:
            # Never overwrite: the earlier content may exist nowhere else any more.
            file_name = entry.name.removesuffix(_SUFFIX) + f".{sha256[:8]}{_SUFFIX}"
        relative = f"{operator}/{file_name}"
        self._write_file(self._directory / relative, data)
        index_entry = IndexEntry(
            stored_at=self._clock(),
            operator=operator,
            name=entry.name,
            file=relative,
            bytes=len(data),
            sha256=sha256,
            listed_modified=entry.modified,
        )
        self._append_index(index_entry)
        if versions:
            log.warning(
                "%s/%s was republished with new content; stored as %s",
                operator,
                entry.name,
                file_name,
            )
            return StoreOutcome.REPUBLISHED
        log.info("Stored %s (%d bytes)", relative, len(data))
        return StoreOutcome.STORED

    def _write_file(self, path: Path, data: bytes) -> None:
        if path.exists():
            raise FileExistsError(f"{path} exists but is not in {INDEX_NAME}")
        path.parent.mkdir(exist_ok=True)
        partial = path.parent / f".{path.name}.part"
        with partial.open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(partial, path)
        _fsync_directory(path.parent)

    def _append_index(self, entry: IndexEntry) -> None:
        with self._index_path.open("a", encoding="utf-8") as file:
            file.write(entry.to_json() + "\n")
            file.flush()
            os.fsync(file.fileno())
        self._entries.append(entry)

    def _remove_partial_downloads(self) -> None:
        for partial in self._directory.glob("*/.*.part"):
            log.warning("Removing unfinished download %s", partial.relative_to(self._directory))
            partial.unlink()
        temporary = self._directory / f".{INDEX_NAME}.tmp"
        if temporary.exists():
            log.warning("Removing unfinished rewrite of %s", INDEX_NAME)
            temporary.unlink()

    def _read_index(self) -> list[IndexEntry]:
        if not self._index_path.exists():
            return []
        text = self._index_path.read_text(encoding="utf-8")
        lines = text.split("\n")
        if lines[-1]:
            # A crash while appending leaves a last line without its newline. The file it
            # described is still on disk and gets adopted below.
            log.warning("Removing cut-off last line of %s", INDEX_NAME)
            self._replace_index(text[: len(text) - len(lines[-1])])
        entries = []
        for number, line in enumerate(lines[:-1], start=1):
            try:
                entries.append(IndexEntry.from_json(line))
            except (ValueError, KeyError, TypeError) as exc:
                message = f"{INDEX_NAME} line {number} is invalid: {exc}"
                raise ArchiveMismatchError(message) from None
        return entries

    def _replace_index(self, text: str) -> None:
        # Rewritten beside the index and swapped in, so a crash here can't lose the index.
        temporary = self._directory / f".{INDEX_NAME}.tmp"
        with temporary.open("w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self._index_path)
        _fsync_directory(self._directory)

    def _check_files(self) -> None:
        for entry in self._entries:
            path = self._directory / entry.file
            if not path.is_file():
                raise ArchiveMismatchError(f"{entry.file} is in {INDEX_NAME} but missing")
            if path.stat().st_size != entry.bytes:
                raise ArchiveMismatchError(
                    f"{entry.file} has {path.stat().st_size} bytes, {INDEX_NAME} says {entry.bytes}"
                )

    def _adopt_unindexed_files(self) -> None:
        indexed = {entry.file for entry in self._entries}
        # Originals first, so a republished copy can be matched to its original's name.
        paths = sorted(
            self._directory.glob("*/*"), key=lambda p: (bool(_REPUBLISHED.match(p.name)), p)
        )
        for path in paths:
            relative = path.relative_to(self._directory).as_posix()
            if relative in indexed or path.name.startswith("."):
                continue
            if (
                not path.is_file()
                or not NETEX_NAME.match(path.name)
                or not OPERATOR.match(path.parent.name)
            ):
                log.warning("Leaving unexpected entry in timetable archive alone: %s", relative)
                continue
            data = path.read_bytes()
            entry = IndexEntry(
                stored_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                operator=path.parent.name,
                name=_original_name(path.name, self._entries, path.parent.name),
                file=relative,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                listed_modified=None,
            )
            log.warning("Adding %s to %s; it was stored but never indexed", relative, INDEX_NAME)
            self._append_index(entry)


def _original_name(file_name: str, entries: list[IndexEntry], operator: str) -> str:
    # A republished version is stored as "<name>.<hash8>.xml.gz"; its index name is the original.
    match = _REPUBLISHED.match(file_name)
    if match is not None:
        original = match["original"] + _SUFFIX
        if any(e.operator == operator and e.name == original for e in entries):
            return original
    return file_name


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _str(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an integer")
    return value
