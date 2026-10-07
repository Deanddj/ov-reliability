import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ov_reliability.collector.__main__ import main
from ov_reliability.collector.archive import list_archive, read_archive
from ov_reliability.collector.records import Marker

CONFIG = """
[source]
endpoint = "tcp://127.0.0.1:9"
topics = ["/RIG/KV6posinfo"]

[archive]
directory = "{directory}"
retention_days = 30
flush_seconds = 1
disk_warning_gb = 0

[monitoring]
service_hours = ["06:00", "00:00"]
topic_silence_minutes = 15
restart_silence_minutes = 10
"""


def test_missing_config_exits_with_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OV_CONFIG", str(tmp_path / "missing.toml"))
    assert main() == 2
    assert "missing.toml" in caplog.text


def test_sigterm_writes_stopped_marker_and_exits_0(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(directory=archive), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "ov_reliability.collector"],
        env={**os.environ, "OV_CONFIG": str(config)},
    )
    try:
        deadline = time.monotonic() + 10
        while not (archive.exists() and list_archive(archive)):
            assert time.monotonic() < deadline, "recorder did not start"
            time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()

    records = [r for f in list_archive(archive) for r in read_archive(f.path)]
    found = [r for r in records if isinstance(r, Marker)]
    assert found[0].event == "started"
    assert found[-1].event == "stopped"
    assert found[-1].details["reason"] == "signal"
