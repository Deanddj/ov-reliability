import pytest
from conftest import FIXTURES

from ov_reliability.timetables.listing import Entry, ListingFormatError, parse_listing


def _row(href: str, date: str = "08-Oct-2026 03:00") -> str:
    return (
        f'<tr><td><img alt="[   ]"></td><td><a class="link" href="{href}">{href}</a></td>'
        f'<td><span class="date">{date}</span></td><td class="size">1M</td></tr>'
    )


def _page(*rows: str) -> str:
    parent = _row("../", "")
    return f"<html><body><table><tbody>{parent}{''.join(rows)}</tbody></table></body></html>"


def test_parses_listing_with_many_files() -> None:
    entries = parse_listing((FIXTURES / "listing_cxx.html").read_text(encoding="utf-8"))
    assert len(entries) == 16
    assert (
        Entry("NeTEx_CXX_AML_20261007_2026-10-08_202600236_9.3.0.xml.gz", "07-Oct-2026 15:56")
        in entries
    )
    assert any("_vehicles_" in entry.name for entry in entries)


def test_parses_listing_with_one_file() -> None:
    entries = parse_listing((FIXTURES / "listing_gvb.html").read_text(encoding="utf-8"))
    assert entries == [
        Entry("NeTEx_GVB_GVB_20260930T180600_20261005_prod_prodexpl.xml.gz", "01-Oct-2026 17:16")
    ]


def test_empty_directory_gives_no_entries() -> None:
    assert parse_listing(_page()) == []


def test_page_without_listing_rows_is_a_format_error() -> None:
    with pytest.raises(ListingFormatError):
        parse_listing("<html><body><p>Maintenance</p></body></html>")


@pytest.mark.parametrize(
    "href",
    [
        "../NeTEx_x.xml.gz",
        "NeTEx_a/b.xml.gz",
        "NeTEx_x.xml.gz.exe",
        "/netex/gvb/NeTEx_x.xml.gz",
        "https://example.org/NeTEx_x.xml.gz",
        "?order=N",
    ],
)
def test_rejects_unsafe_names(href: str) -> None:
    assert parse_listing(_page(_row(href))) == []
