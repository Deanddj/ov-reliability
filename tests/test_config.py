import copy
from datetime import time
from pathlib import Path
from typing import Any

import pytest
from conftest import config_data
from pydantic import ValidationError

from ov_reliability.config import Config, TimetablesConfig, load_config, load_timetables_config

REPO_ROOT = Path(__file__).parents[1]


def test_repository_config_is_valid() -> None:
    config = load_config(REPO_ROOT / "config.toml")
    assert config.archive.retention_days == 60
    assert config.monitoring.service_hours == (time(6, 0), time(0, 0))
    assert "/RIG/KV6posinfo" in config.source.topics
    timetables = load_timetables_config(REPO_ROOT / "config.toml")
    assert timetables.check_minutes == 60
    assert "htm" in timetables.operators


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
