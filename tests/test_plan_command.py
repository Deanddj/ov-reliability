from pathlib import Path

import pytest
from netexfile import NAME, write_netex

from ov_reliability.timestamps import parse_timestamp
from ov_reliability.timetables.plan import main
from ov_reliability.timetables.store import IndexEntry

CONFIG = """
[timetables]
base_url = "https://data.ndovloket.nl/netex/"
operators = ["htm"]
directory = "{directory}"
check_minutes = 60
timeout_seconds = 120
disk_warning_gb = 10
"""


@pytest.fixture(autouse=True)
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "timetables"
    write_netex(directory)
    entry = IndexEntry(
        stored_at=parse_timestamp("2026-10-08T12:00:00Z"),
        operator="htm",
        name=NAME,
        file=f"htm/{NAME}",
        bytes=1,
        sha256="0" * 64,
        listed_modified=None,
    )
    (directory / "index.jsonl").write_text(entry.to_json() + "\n")
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(directory=directory))
    monkeypatch.setenv("OV_CONFIG", str(config))


def test_prints_the_trips(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["htm", "tram", "3", "2026-10-11"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"htm tram 3, 2026-10-11, files: {NAME}",
        "02:30  journey 3004    pn 3    inbound   Den Haag Loosduinen      02:30 -> 02:35  3 stops",
        "05:31  journey 3001    pn 3    outbound  Zoetermeer Centrum       05:31 -> 05:36  3 stops",
        "07:00  journey 53001   pn 53   outbound  Zoetermeer Centrum       07:00 -> 07:05  2 stops",
        "23:50  journey 3002    pn 3    inbound   Den Haag Loosduinen      23:50 -> 23:55  3 stops",
        "00:10  journey 3003    pn 3    outbound  Zoetermeer Centrum       00:10 -> 00:15  3 stops",
        "inbound 2 trips, outbound 3 trips",
    ]


def test_prints_the_stops_of_a_journey(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["htm", "tram", "3", "2026-10-11", "--journey", "3001"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "journey 3001  pn 3  outbound  Zoetermeer Centrum",
        "  1  1001     Alpha                            05:31:00  05:31:00",
        "  2  1002     Beta                             05:33:36  05:34:36",
        "  3  1003     Gamma                            05:36:36  05:36:36",
    ]


def test_unknown_journey_exits_with_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["htm", "tram", "3", "2026-10-11", "--journey", "999"]) == 1
    assert "Journey 999 is not planned on 2026-10-11" in capsys.readouterr().err


def test_day_without_a_timetable_exits_with_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["htm", "tram", "3", "2026-10-12"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no timetable file of htm covers 2026-10-12" in captured.err


def test_bad_config_exits_with_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OV_CONFIG", str(tmp_path / "missing.toml"))
    assert main(["htm", "tram", "3", "2026-10-11"]) == 2
    assert "missing.toml" in capsys.readouterr().err


def test_bad_day_exits_with_2() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["htm", "tram", "3", "11-10-2026"])
    assert exit_info.value.code == 2
