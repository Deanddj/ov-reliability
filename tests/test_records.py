from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ov_reliability.collector.records import (
    RECORDER_TOPIC,
    BadPayload,
    InvalidRecordError,
    Marker,
    Message,
    decode,
    encode,
)

AT = datetime(2026, 10, 7, 14, 30, 5, 123000, tzinfo=UTC)


@pytest.mark.parametrize(
    "record",
    [
        Message(AT, "/RIG/KV6posinfo", "<xml>é</xml>"),
        BadPayload(AT, "/GVB/KV6posinfo", "AAEC", "BadGzipFile: Not a gzipped file"),
        Marker(AT, "started", {"run_id": "r1", "topics": ["/RIG/KV6posinfo"]}),
    ],
)
def test_round_trip(record: Message | BadPayload | Marker) -> None:
    assert decode(encode(record)) == record


def test_marker_uses_recorder_topic() -> None:
    assert f'"topic":"{RECORDER_TOPIC}"' in encode(Marker(AT, "stopped", {"reason": "signal"}))


def test_marker_details_cannot_shadow_fields() -> None:
    with pytest.raises(ValueError):
        Marker(AT, "started", {"topic": "x"})


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"topic": "/RIG/KV6posinfo", "payload": "x"}',
        '{"received_at": "2026-10-07T14:30:05.123Z", "topic": "/RIG/KV6posinfo"}',
        '{"received_at": "2026-10-07T14:30:05.123Z", "topic": "/RIG/KV6posinfo", "payload": 1}',
        '{"received_at": "2026-10-07T14:30:05.123", "topic": "/RIG/KV6posinfo", "payload": "x"}',
        '{"received_at": "2026-10-07T14:30:05.123Z", "topic": "_recorder"}',
        '{"received_at": "2026-10-07T14:30:05.123Z", "topic": "/A", "payload": "x", "extra": 1}',
    ],
)
def test_invalid_lines_are_rejected(line: str) -> None:
    with pytest.raises(InvalidRecordError):
        decode(line)


@given(topic=st.text(min_size=1).filter(lambda t: t != RECORDER_TOPIC), payload=st.text())
def test_any_message_round_trips_on_one_line(topic: str, payload: str) -> None:
    line = encode(Message(AT, topic, payload))
    assert "\n" not in line
    assert decode(line) == Message(AT, topic, payload)
