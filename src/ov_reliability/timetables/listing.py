"""Parse the NeTEx directory listings of data.ndovloket.nl."""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Names are used as file names, so anything that could leave the operator directory is refused.
NETEX_NAME = re.compile(r"^NeTEx_[A-Za-z0-9_.\-]+\.xml\.gz$")


@dataclass(frozen=True)
class Entry:
    name: str
    modified: str


class ListingFormatError(Exception):
    pass


def parse_listing(html: str) -> list[Entry]:
    parser = _ListingParser()
    parser.feed(html)
    parser.close()
    # Even an empty directory lists "Parent Directory" with a date cell; without any such row
    # the page is not the listing we know, and an empty result would hide that.
    if not parser.rows:
        raise ListingFormatError("no rows with a link and a date")
    return [Entry(href, modified) for href, modified in parser.rows if NETEX_NAME.match(href)]


class _ListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str]] = []
        self._href: str | None = None
        self._date: list[str] | None = None
        self._in_date = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self._href, self._date = None, None
        elif tag == "a" and self._href is None:
            self._href = attributes.get("href")
        elif tag == "span" and attributes.get("class") == "date":
            self._date, self._in_date = [], True

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._in_date = False
        elif tag == "tr":
            if self._href is not None and self._date is not None:
                self.rows.append((self._href, "".join(self._date).strip()))
            self._href, self._date = None, None

    def handle_data(self, data: str) -> None:
        if self._in_date and self._date is not None:
            self._date.append(data)
