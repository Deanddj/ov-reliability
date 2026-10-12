import copy
import re
from datetime import time
from pathlib import Path
from typing import Any

import pytest
from conftest import config_data
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from ov_reliability.config import (
    ApiConfig,
    Config,
    LinePattern,
    LoaderConfig,
    TimetablesConfig,
    load_api_config,
    load_config,
    load_loader_config,
    load_timetables_config,
)

REPO_ROOT = Path(__file__).parents[1]


def test_repository_config_is_valid() -> None:
    config = load_config(REPO_ROOT / "config.toml")
    assert config.archive.retention_days == 60
    assert config.monitoring.service_hours == (time(6, 0), time(0, 0))
    assert "/RIG/KV6posinfo" in config.source.topics
    timetables = load_timetables_config(REPO_ROOT / "config.toml")
    assert timetables.check_minutes == 60
    assert "htm" in timetables.operators
    loader = load_loader_config(REPO_ROOT / "config.toml")
    assert loader.data_owners == ("HTM", "GVB", "RET", "CXX")
    assert [archive.recorder for archive in loader.archives] == ["collector"]
    api = load_api_config(REPO_ROOT / "config.toml")
    assert LinePattern("htm", "tram", "3") in api.lines
    assert api.ignore_planning_numbers == (("htm", "54"),)


def test_siri_config_is_valid() -> None:
    config = load_config(REPO_ROOT / "config.siri.toml")
    assert config.source.endpoint.endswith(":7666")
    assert config.monitoring.watched_topics == ("/GVB/EstimatedTimetableDelivery",)
    assert config.monitoring.service_hours == (time(7, 30), time(0, 30))


def test_load_config_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[source]
endpoint = "tcp://example:1"
topics = ["/RIG/KV6posinfo"]

[archive]
directory = "/data/archive"
retention_days = 7
flush_seconds = 5
disk_warning_gb = 10

[monitoring]
service_hours = ["06:00", "00:00"]
topic_silence_minutes = 15
restart_silence_minutes = 10
watched_topics = ["/RIG/KV6posinfo"]

[timetables]
base_url = "https://example.org/netex/"
operators = ["gvb", "htm"]
directory = "/data/timetables"
check_minutes = 60
timeout_seconds = 30
disk_warning_gb = 0
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.source.topics == ("/RIG/KV6posinfo",)
    assert config.archive.directory == Path("/data/archive")
    assert config.archive.retention_days == 7
    timetables = load_timetables_config(path)
    assert timetables.operators == ("gvb", "htm")
    assert timetables.directory == Path("/data/timetables")


def test_repo_config_watches_the_kv6_topics() -> None:
    config = load_config(REPO_ROOT / "config.toml")
    kv6 = {topic for topic in config.source.topics if topic.endswith("/KV6posinfo")}
    assert len(kv6) == 8
    assert set(config.monitoring.watched_topics) == kv6


@pytest.mark.parametrize(
    ("watched", "message"),
    [
        ([], "at least 1"),
        (["/RIG/KV6posinfo", "/RIG/KV6posinfo"], "unique"),
        (["/HTM/KV6posinfo"], "'/HTM/KV6posinfo' is not in source.topics"),
    ],
)
def test_invalid_watched_topics_are_rejected(watched: list[str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Config.model_validate(_with(("monitoring", "watched_topics"), watched))


def test_missing_watched_topics_is_an_error() -> None:
    data = copy.deepcopy(config_data(Path("/tmp/archive")))
    del data["monitoring"]["watched_topics"]
    with pytest.raises(ValidationError, match="watched_topics"):
        Config.model_validate(data)


def _with(path: tuple[str, str], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(config_data(Path("/tmp/archive")))
    data[path[0]][path[1]] = value
    return data


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("archive", "retention_days"), 0),
        (("archive", "flush_seconds"), 0),
        (("archive", "disk_warning_gb"), -1),
        (("source", "topics"), []),
        (("source", "topics"), ["/RIG/KV6posinfo", "/RIG/KV6posinfo"]),
        (("source", "topics"), [""]),
        (("source", "endpoint"), ""),
        (("monitoring", "service_hours"), ["6am", "00:00"]),
        (("monitoring", "service_hours"), ["06:00"]),
        (("monitoring", "topic_silence_minutes"), 0),
        (("monitoring", "restart_silence_minutes"), 0),
        (("archive", "unexpected"), 1),
    ],
)
def test_invalid_values_are_rejected(path: tuple[str, str], value: Any) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(_with(path, value))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("base_url", "http://example.org/netex/"),
        ("base_url", "https://example.org/netex"),
        ("operators", []),
        ("operators", ["gvb", "gvb"]),
        ("operators", ["GVB"]),
        ("operators", ["../gvb"]),
        ("check_minutes", 14),
        ("timeout_seconds", 0),
        ("disk_warning_gb", -1),
        ("unexpected", 1),
    ],
)
def test_invalid_timetables_values_are_rejected(key: str, value: Any) -> None:
    data = copy.deepcopy(config_data(Path("/tmp/archive"))["timetables"])
    data[key] = value
    with pytest.raises(ValidationError):
        TimetablesConfig.model_validate(data)


def test_bad_timetables_section_does_not_stop_the_collector() -> None:
    data = copy.deepcopy(config_data(Path("/tmp/archive")))
    data["timetables"]["operators"] = ["GVB"]
    Config.model_validate(data)


def test_missing_timetables_section_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[source]\nendpoint = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="timetables"):
        load_timetables_config(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("data_owners", []),
        ("data_owners", ["htm"]),
        ("data_owners", ["HTM", "HTM"]),
        ("window_days", 0),
        ("poll_seconds", 0),
        ("archives", []),
        ("archives", [{"recorder": "Collector", "directory": "/data/archive"}]),
        (
            "archives",
            [
                {"recorder": "collector", "directory": "/data/a"},
                {"recorder": "collector", "directory": "/data/b"},
            ],
        ),
        ("unexpected", 1),
    ],
)
def test_invalid_loader_values_are_rejected(key: str, value: Any) -> None:
    data = copy.deepcopy(config_data(Path("/tmp/archive"))["loader"])
    data[key] = value
    with pytest.raises(ValidationError):
        LoaderConfig.model_validate(data)


def test_missing_loader_section_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[source]\nendpoint = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="loader"):
        load_loader_config(path)


def _api(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = copy.deepcopy(config_data(Path("/tmp/archive"))["api"])
    data.update(changes)
    return data


def test_api_section_parses_patterns() -> None:
    api = ApiConfig.model_validate(_api(lines=["htm/tram/3", "htm/*/*"]))
    assert api.lines == (LinePattern("htm", "tram", "3"), LinePattern("htm", "*", "*"))
    assert str(api.lines[1]) == "htm/*/*"
    assert api.no_realtime == (LinePattern("gvb", "ferry", "*"),)
    assert api.realtime[0].lines == (LinePattern("htm", "*", "*"),)


def test_no_realtime_may_carve_out_of_a_realtime_entry() -> None:
    # Widening to all of HTM: the realtime entry covers htm/*/*, the ferries have none.
    ApiConfig.model_validate(_api(lines=["htm/*/*"], no_realtime=["htm/ferry/*"]))


BAD_PATTERNS = [
    "htm/tram",
    "htm/tram/3/4",
    "*/tram/3",
    "HTM/tram/3",
    "htm/Tram/3",
    "htm/tram/",
    "htm//3",
    "htm/tram/3 ",
    "htm/tram/3*",
    "",
]


@pytest.mark.parametrize("pattern", BAD_PATTERNS)
@pytest.mark.parametrize("key", ["lines", "no_realtime"])
def test_bad_line_patterns_are_refused(key: str, pattern: str) -> None:
    with pytest.raises(ValidationError, match="operator/mode/code"):
        ApiConfig.model_validate(_api(**{key: [pattern]}))


@pytest.mark.parametrize("pattern", BAD_PATTERNS)
def test_bad_realtime_patterns_are_refused(pattern: str) -> None:
    data = _api()
    data["realtime"][0]["lines"] = [pattern]
    with pytest.raises(ValidationError, match="operator/mode/code"):
        ApiConfig.model_validate(data)


@pytest.mark.parametrize("entry", ["htm", "htm/", "/54", "htm/5/4", "HTM/54", "htm/5 4", 54])
def test_bad_ignored_planning_numbers_are_refused(entry: object) -> None:
    with pytest.raises(ValidationError, match="operator/number"):
        ApiConfig.model_validate(_api(ignore_planning_numbers=[entry]))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("default_range_minutes", 0),
        ("max_range_days", 0),
        ("missing_after_minutes", 0),
        ("lines", []),
        ("unexpected", 1),
    ],
)
def test_invalid_api_values_are_refused(key: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        ApiConfig.model_validate(_api(**{key: value}))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("lines", []),
        ("recorder", "Collector"),
        ("topics", []),
        ("topics", [""]),
        ("topics", ["/RIG/KV6posinfo", "/RIG/KV6posinfo"]),
    ],
)
def test_invalid_realtime_values_are_refused(key: str, value: Any) -> None:
    data = _api()
    data["realtime"][0][key] = value
    with pytest.raises(ValidationError):
        ApiConfig.model_validate(data)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"lines": ["ret/tram/3"]},
            "ret/tram/3 is in no [[api.realtime]] entry nor in no_realtime",
        ),
        ({"lines": ["gvb/*/*"]}, "gvb/*/* is in no [[api.realtime]] entry nor in no_realtime"),
        ({"no_realtime": ["htm/tram/*"]}, "htm/tram/3 is in an [[api.realtime]] entry and in"),
    ],
)
def test_every_line_has_exactly_one_kind_of_source(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        ApiConfig.model_validate(_api(**changes))


def test_a_line_without_realtime_data_is_served() -> None:
    ApiConfig.model_validate(_api(lines=["htm/tram/3", "gvb/ferry/902"]))


def test_overlapping_realtime_entries_are_refused() -> None:
    data = _api()
    data["realtime"].append(
        {"lines": ["htm/tram/*"], "recorder": "collector", "topics": ["/RIG/KV6posinfo"]}
    )
    with pytest.raises(ValidationError, match=re.escape("htm/*/* and htm/tram/* overlap")):
        ApiConfig.model_validate(data)


PARTS = st.sampled_from(["tram", "bus", "*"]), st.sampled_from(["3", "4", "*"])
PATTERNS = st.builds(LinePattern, st.sampled_from(["htm", "ret"]), *PARTS)
# "other" stands for every mode or code no pattern names.
LINES = [
    LinePattern(operator, mode, code)
    for operator in ("htm", "ret")
    for mode in ("tram", "bus", "other")
    for code in ("3", "4", "other")
]


@given(PATTERNS, PATTERNS)
def test_covers_and_overlaps_agree_with_the_lines_matched(a: LinePattern, b: LinePattern) -> None:
    in_a = {line for line in LINES if a.covers(line)}
    in_b = {line for line in LINES if b.covers(line)}
    assert a.covers(b) == (in_b <= in_a)
    assert a.overlaps(b) == bool(in_a & in_b) == b.overlaps(a)


def _repo_config_with(tmp_path: Path, *changes: tuple[str, str]) -> Path:
    text = (REPO_ROOT / "config.toml").read_text(encoding="utf-8")
    for old, new in changes:
        assert old in text
        text = text.replace(old, new, 1)
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_api_line_operator_must_have_timetables(tmp_path: Path) -> None:
    path = _repo_config_with(
        tmp_path,
        ('lines = ["htm/tram/3"', 'lines = ["abc/bus/1", "htm/tram/3"'),
        ("no_realtime = []", 'no_realtime = ["abc/*/*"]'),
    )
    with pytest.raises(ValueError, match=re.escape("abc/bus/1: operator not in [timetables]")):
        load_api_config(path)


def test_api_recorder_must_be_loaded(tmp_path: Path) -> None:
    path = _repo_config_with(tmp_path, ('recorder = "collector"\ntopics', 'recorder = "x"\ntopics'))
    with pytest.raises(ValueError, match=re.escape("recorder 'x' is not in [[loader.archives]]")):
        load_api_config(path)


@pytest.mark.parametrize("section", ["api", "timetables", "loader"])
def test_api_needs_its_sections(tmp_path: Path, section: str) -> None:
    kept = []
    skipping = False
    for line in (REPO_ROOT / "config.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            skipping = line.strip("[]").split(".")[0] == section
        if not skipping:
            kept.append(line)
    path = tmp_path / "config.toml"
    path.write_text("\n".join(kept), encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(f"no [{section}] section")):
        load_api_config(path)
