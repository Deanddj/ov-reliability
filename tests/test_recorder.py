import gzip
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES, config_data

from ov_reliability.collector.archive import ArchiveWriter, list_archive, read_archive
from ov_reliability.collector.recorder import EXIT_OK, EXIT_SILENCE, Recorder
from ov_reliability.collector.records import BadPayload, Marker, Message, Record
from ov_reliability.collector.stream import Connection, Event, Received, Tick
from ov_reliability.config import Config

T0 = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)  # 16:00 in Amsterdam, within service hours
RIG = "/RIG/KV6posinfo"
GVB = "/GVB/KV6posinfo"
XML = (FIXTURES / "kv6_rig.xml").read_text(encoding="utf-8")


def received(at: datetime, topic: str = RIG, payload: bytes | None = None) -> Received:
    data = gzip.compress(XML.encode()) if payload is None else payload
    return Received(at, topic.encode(), (data,))


def run(
    config: Config, events: Iterable[Event], stop_at_end: bool = True
) -> tuple[int, list[Record]]:
    directory = config.archive.directory
    directory.mkdir(parents=True, exist_ok=True)
    stop = False

    def stream() -> Iterator[Event]:
        nonlocal stop
        last = T0
        for event in events:
            last = event_time(event)
            yield event
        if stop_at_end:
            stop = True
            yield Tick(last)

    writer = ArchiveWriter(directory, config.archive.flush_seconds)
    recorder = Recorder(config, writer, lambda: T0, lambda: stop, "0.1.0", run_id="run-1")
    code = recorder.run(stream())
    records = [r for f in list_archive(directory) for r in read_archive(f.path)]
    return code, records


def event_time(event: Event) -> datetime:
    if isinstance(event, Received):
        return event.received_at
    if isinstance(event, Connection):
        return event.at
    return event.now


def markers(records: list[Record], event: str) -> list[Marker]:
    return [r for r in records if isinstance(r, Marker) and r.event == event]


def test_started_and_stopped_markers(config: Config) -> None:
    code, records = run(config, [received(T0)])
    assert code == EXIT_OK
    first, last = records[0], records[-1]
    assert isinstance(first, Marker) and first.event == "started"
    assert first.details == {
        "run_id": "run-1",
        "version": "0.1.0",
        "endpoint": config.source.endpoint,
        "topics": list(config.source.topics),
    }
    assert isinstance(last, Marker) and last.event == "stopped"
    assert last.details == {"run_id": "run-1", "reason": "signal"}


def test_message_is_stored_decompressed(config: Config) -> None:
    _, records = run(config, [received(T0)])
    assert [r for r in records if isinstance(r, Message)] == [Message(T0, RIG, XML)]


def test_bad_payload_is_kept_and_recording_continues(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _, records = run(config, [received(T0, payload=b"\x00\x01"), received(T0)])
    bad = [r for r in records if isinstance(r, BadPayload)]
    assert len(bad) == 1 and bad[0].payload_b64 == "AAE="
    assert [r for r in records if isinstance(r, Message)] == [Message(T0, RIG, XML)]
    assert "bad payload" in caplog.text.lower()


def test_topic_that_is_not_utf8_becomes_bad_payload(config: Config) -> None:
    event = Received(T0, b"/RIG/\xff", (gzip.compress(b"<x/>"),))
    _, records = run(config, [event])
    bad = [r for r in records if isinstance(r, BadPayload)]
    assert len(bad) == 1 and "UTF-8" in bad[0].error


def test_unlisted_topic_is_recorded(config: Config) -> None:
    _, records = run(config, [received(T0, topic="/RIG/KV6posinfoExtra")])
    assert [r.topic for r in records if isinstance(r, Message)] == ["/RIG/KV6posinfoExtra"]


def test_connection_events_become_markers(config: Config) -> None:
    _, records = run(config, [Connection("connected", T0), Connection("disconnected", T0)])
    assert len(markers(records, "connected")) == 1
    assert markers(records, "disconnected")[0].details["run_id"] == "run-1"


def test_one_silent_topic_gets_start_and_end_markers(config: Config) -> None:
    events: list[Event] = [received(T0, GVB)]
    events += [received(T0 + timedelta(minutes=m), RIG) for m in range(1, 21)]
    events.append(received(T0 + timedelta(minutes=21), GVB))
    code, records = run(config, events)
    assert code == EXIT_OK
    assert [m.details["silent_topic"] for m in markers(records, "silence_start")] == [GVB]
    assert [m.details["silent_topic"] for m in markers(records, "silence_end")] == [GVB]


def test_all_topics_silent_exits_with_1(config: Config) -> None:
    code, records = run(config, [Tick(T0 + timedelta(minutes=11))], stop_at_end=False)
    assert code == EXIT_SILENCE
    assert markers(records, "stopped")[0].details["reason"] == "silence"


def test_rollover_logs_health_line_and_closes_file(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        run(config, [received(T0 + timedelta(minutes=59)), Tick(T0 + timedelta(minutes=60))])
    assert "2026-10-07T14Z: 3 records" in caplog.text
    assert f"{RIG}=1" in caplog.text


def test_first_message_of_a_new_hour_is_not_in_the_previous_health_line(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        run(config, [received(T0 + timedelta(minutes=30)), received(T0 + timedelta(minutes=60))])
    lines = [m for m in caplog.messages if m.startswith("2026-10-07T14Z:")]
    assert len(lines) == 1
    assert f"{RIG}=1" in lines[0] and f"{RIG}=2" not in lines[0]


def test_low_disk_warns_instead_of_info_health_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = config_data(tmp_path / "archive")
    data["archive"]["disk_warning_gb"] = 10**9
    config = Config.model_validate(data)
    with caplog.at_level(logging.INFO):
        run(config, [received(T0), Tick(T0 + timedelta(minutes=60))])
    health = [r for r in caplog.records if "T14Z:" in r.getMessage()]
    assert [r.levelno for r in health] == [logging.WARNING]
    assert "free disk" in health[0].getMessage().lower()


def test_unseen_topics_are_reported_after_an_hour(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    events = [
        received(T0 + timedelta(minutes=m), topic) for m in range(0, 65, 5) for topic in (RIG, GVB)
    ]
    with caplog.at_level(logging.WARNING):
        run(config, events)
    assert "/RIG/KV17cvlinfo" in caplog.text


def test_retention_runs_at_startup(config: Config) -> None:
    config.archive.directory.mkdir(parents=True)
    old = config.archive.directory / "2026-08-01T00Z.0.jsonl.gz"
    old.write_bytes(b"")
    run(config, [received(T0)])
    assert not old.exists()


def test_stream_ending_without_stop_is_an_error(config: Config) -> None:
    with pytest.raises(RuntimeError):
        run(config, [received(T0)], stop_at_end=False)
