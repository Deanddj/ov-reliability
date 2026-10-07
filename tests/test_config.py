import copy
from datetime import time
from pathlib import Path
from typing import Any

import pytest
from conftest import config_data
from pydantic import ValidationError

from ov_reliability.config import Config, load_config

REPO_ROOT = Path(__file__).parents[1]


def test_repository_config_is_valid() -> None:
    config = load_config(REPO_ROOT / "config.toml")
    assert config.archive.retention_days == 30
    assert config.monitoring.service_hours == (time(6, 0), time(0, 0))
    assert "/RIG/KV6posinfo" in config.source.topics


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
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.source.topics == ("/RIG/KV6posinfo",)
    assert config.archive.directory == Path("/data/archive")
    assert config.archive.retention_days == 7


def test_kv6_topics_only_lists_kv6(config: Config) -> None:
    assert config.kv6_topics == ("/RIG/KV6posinfo", "/GVB/KV6posinfo")


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
