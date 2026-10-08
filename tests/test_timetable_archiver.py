import gzip
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import FIXTURES

from ov_reliability.config import TimetablesConfig
from ov_reliability.timetables.archiver import Archiver, FetchError, http_fetch
from ov_reliability.timetables.store import INDEX_NAME, TimetableStore

NOW = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
BASE = "https://example.org/netex/"


def netex(body: str = "a") -> bytes:
    return gzip.compress(f'<?xml version="1.0"?><P>{body}</P>'.encode(), mtime=0)


def listing(*names: str) -> bytes:
    rows = "".join(
        f'<tr><td></td><td><a class="link" href="{name}">{name}</a></td>'
        f'<td><span class="date">08-Oct-2026 03:00</span></td></tr>'
        for name in ("../", *names)
    )
    return f"<table>{rows}</table>".encode()


class FakeServer:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requests: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        if url not in self.files:
            raise FetchError("HTTPError: 404")
        return self.files[url]


def settings(directory: Path, operators: tuple[str, ...] = ("gvb", "htm")) -> TimetablesConfig:
    return TimetablesConfig(
        base_url=BASE,
        operators=operators,
        directory=directory,
        check_minutes=60,
        timeout_seconds=5,
        disk_warning_gb=1,
    )


def archiver(directory: Path, server: FakeServer, free_bytes: int = 10**12) -> Archiver:
    store = TimetableStore(directory, lambda: NOW)
    store.open()
    return Archiver(settings(directory), store, server, lambda: free_bytes)


def test_check_stores_new_files_once(tmp_path: Path) -> None:
    server = FakeServer(
        {
            BASE + "gvb/": listing("NeTEx_GVB_1.xml.gz"),
            BASE + "gvb/NeTEx_GVB_1.xml.gz": netex(),
            BASE + "htm/": listing("NeTEx_HTM_Bus_1.xml.gz", "NeTEx_HTM_Rail_1.xml.gz"),
            BASE + "htm/NeTEx_HTM_Bus_1.xml.gz": netex("bus"),
            BASE + "htm/NeTEx_HTM_Rail_1.xml.gz": netex("rail"),
        }
    )
    subject = archiver(tmp_path, server)
    first = subject.check(lambda: False)
    assert (first.operators, first.listed, first.new, first.errors) == (2, 3, 3, 0)
    assert (tmp_path / "htm" / "NeTEx_HTM_Rail_1.xml.gz").read_bytes() == netex("rail")

    server.requests.clear()
    second = subject.check(lambda: False)
    assert (second.new, second.errors) == (0, 0)
    assert server.requests == [BASE + "gvb/", BASE + "htm/"]


def test_one_failing_operator_does_not_stop_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(
        {
            BASE + "htm/": listing("NeTEx_HTM_Bus_1.xml.gz"),
            BASE + "htm/NeTEx_HTM_Bus_1.xml.gz": netex(),
        }
    )
    summary = archiver(tmp_path, server).check(lambda: False)
    assert (summary.new, summary.errors, summary.failed_operators) == (1, 1, 1)
    assert "Cannot list https://example.org/netex/gvb/" in caplog.text


def test_unrecognised_listing_is_an_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    server = FakeServer({BASE + "gvb/": b"<p>Maintenance</p>", BASE + "htm/": listing()})
    summary = archiver(tmp_path, server).check(lambda: False)
    assert summary.failed_operators == 1
    assert any(
        record.levelno == logging.ERROR and "format not recognised" in record.message
        for record in caplog.records
    )
    assert "No NeTEx files listed for htm" in caplog.text


def test_bad_download_is_retried_at_the_next_check(tmp_path: Path) -> None:
    url = BASE + "gvb/NeTEx_GVB_1.xml.gz"
    server = FakeServer(
        {BASE + "gvb/": listing("NeTEx_GVB_1.xml.gz"), BASE + "htm/": listing(), url: netex()[:-5]}
    )
    subject = archiver(tmp_path, server)
    assert subject.check(lambda: False).errors == 1
    server.files[url] = netex()
    assert subject.check(lambda: False).new == 1
    assert (tmp_path / "gvb" / "NeTEx_GVB_1.xml.gz").read_bytes() == netex()


def test_stop_during_check_ends_after_the_current_file(tmp_path: Path) -> None:
    stop = threading.Event()
    names = [f"NeTEx_GVB_{n}.xml.gz" for n in range(3)]
    server = FakeServer({BASE + "gvb/": listing(*names)})

    def fetch(url: str) -> bytes:
        if url.endswith(".xml.gz"):
            stop.set()
            return netex(url)
        return server(url)

    store = TimetableStore(tmp_path, lambda: NOW)
    store.open()
    summary = Archiver(settings(tmp_path), store, fetch, lambda: 10**12).check(stop.is_set)
    assert summary.new == 1
    assert summary.operators == 1


def test_health_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    server = FakeServer({BASE + "gvb/": listing(), BASE + "htm/": listing()})
    subject = archiver(tmp_path, server)
    subject._report(subject.check(lambda: False))
    assert (
        "check done: 2 operators, 0 listed, 0 new (0.0 MB), 0 republished, 0 errors" in caplog.text
    )


def test_every_operator_failing_is_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    subject = archiver(tmp_path, FakeServer({}))
    subject._report(subject.check(lambda: False))
    assert caplog.records[-1].levelno == logging.ERROR
    assert "every operator failed" in caplog.text


def test_low_disk_is_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    server = FakeServer({BASE + "gvb/": listing(), BASE + "htm/": listing()})
    subject = archiver(tmp_path, server, free_bytes=10**8)
    subject._report(subject.check(lambda: False))
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.text.rstrip().endswith("; low free disk, warning below 1 GB")


def test_stop_during_wait_is_immediate(tmp_path: Path) -> None:
    server = FakeServer({BASE + "gvb/": listing(), BASE + "htm/": listing()})
    subject = archiver(tmp_path, server)
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(subject.run(stop.is_set)))
    thread.start()
    time.sleep(0.2)
    started = time.monotonic()
    stop.set()
    thread.join(timeout=5)
    assert result == [0]
    assert time.monotonic() - started < 2


@pytest.fixture
def http_server() -> Iterator[str]:
    files = {
        "/netex/gvb/": (FIXTURES / "listing_gvb.html").read_bytes(),
        "/netex/gvb/NeTEx_GVB_GVB_20260930T180600_20261005_prod_prodexpl.xml.gz": netex("gvb"),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/netex/"
    finally:
        server.shutdown()
        server.server_close()


def test_real_http_download(tmp_path: Path, http_server: str) -> None:
    store = TimetableStore(tmp_path, lambda: NOW)
    store.open()
    # The config only allows https; the local test server speaks plain http.
    config = settings(tmp_path, ("gvb",)).model_copy(update={"base_url": http_server})
    summary = Archiver(config, store, http_fetch(5), lambda: 10**12).check(lambda: False)
    assert (summary.new, summary.errors) == (1, 0)
    [line] = (tmp_path / INDEX_NAME).read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["listed_modified"] == "01-Oct-2026 17:16"


def test_http_errors_become_fetch_errors(http_server: str) -> None:
    with pytest.raises(FetchError, match="404"):
        http_fetch(5)(http_server + "missing/")


def test_missing_config_exits_with_2(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ov_reliability.timetables"],
        env={**os.environ, "OV_CONFIG": str(tmp_path / "missing.toml")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "missing.toml" in result.stdout


def test_archive_mismatch_exits_with_1(tmp_path: Path) -> None:
    repo_config = (Path(__file__).parents[1] / "config.toml").read_text(encoding="utf-8")
    archive = tmp_path / "timetables"
    archive.mkdir()
    (archive / INDEX_NAME).write_text("not json\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        repo_config.replace('"/data/timetables"', json.dumps(str(archive))), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-m", "ov_reliability.timetables"],
        env={**os.environ, "OV_CONFIG": str(config)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "does not match its index" in result.stdout


def test_every_download_failing_counts_as_a_failed_operator(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(
        {
            BASE + "gvb/": listing("NeTEx_GVB_1.xml.gz"),
            BASE + "htm/": listing("NeTEx_HTM_Bus_1.xml.gz"),
        }
    )
    subject = archiver(tmp_path, server)
    summary = subject.check(lambda: False)
    assert (summary.failed_operators, summary.errors) == (2, 2)
    subject._report(summary)
    assert "every operator failed" in caplog.text


def test_sigterm_stops_the_service_with_0(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    # Port 9 refuses the connection at once, so the service reaches its wait quickly.
    config.write_text(
        f"""
[timetables]
base_url = "https://127.0.0.1:9/netex/"
operators = ["gvb"]
directory = {json.dumps(str(tmp_path / "timetables"))}
check_minutes = 60
timeout_seconds = 5
disk_warning_gb = 0
""",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "ov_reliability.timetables"],
        env={**os.environ, "OV_CONFIG": str(config)},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if "check done" in line:
                break
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        assert "Stopped" in process.stdout.read()
    finally:
        process.kill()
        process.wait()
