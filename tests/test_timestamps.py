from datetime import UTC, datetime, timedelta, timezone

import pytest

from ov_reliability.timestamps import format_timestamp, parse_timestamp

AT = datetime(2026, 10, 7, 14, 30, 5, 123000, tzinfo=UTC)


def test_timestamp_is_utc_with_milliseconds() -> None:
    cest = timezone(timedelta(hours=2))
    assert format_timestamp(AT.astimezone(cest)) == "2026-10-07T14:30:05.123Z"


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError):
        format_timestamp(datetime(2026, 10, 7, 14, 30))


def test_parse_round_trips() -> None:
    assert parse_timestamp(format_timestamp(AT)) == AT


def test_parse_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError):
        parse_timestamp("2026-10-07T14:30:05.123")
