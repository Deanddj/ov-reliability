from datetime import UTC, datetime, time, timedelta

import pytest

from ov_reliability.collector.silence import (
    AMSTERDAM,
    ServiceHours,
    SilenceTracker,
    TopicResumed,
    TopicSilent,
)

DAY = ServiceHours(time(6, 0), time(0, 0))


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=AMSTERDAM)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (local(2026, 10, 7, 5, 59), None),
        (local(2026, 10, 7, 6, 0), local(2026, 10, 7, 6, 0)),
        (local(2026, 10, 7, 23, 59), local(2026, 10, 7, 6, 0)),
        (local(2026, 10, 8, 0, 0), None),
    ],
)
def test_window_until_midnight(now: datetime, expected: datetime | None) -> None:
    assert DAY.window_start(now) == expected


def test_window_past_midnight_starts_yesterday() -> None:
    evening = ServiceHours(time(18, 0), time(2, 0))
    assert evening.window_start(local(2026, 10, 8, 1, 0)) == local(2026, 10, 7, 18, 0)
    assert evening.window_start(local(2026, 10, 8, 3, 0)) is None


def test_window_within_one_day() -> None:
    office = ServiceHours(time(9, 0), time(17, 0))
    assert office.window_start(local(2026, 10, 7, 12, 0)) == local(2026, 10, 7, 9, 0)
    assert office.window_start(local(2026, 10, 7, 17, 0)) is None


@pytest.mark.parametrize(
    ("day", "utc_hour"),
    [((2026, 10, 25), 5), ((2026, 3, 29), 4)],  # 06:00 CET = 05:00 UTC, 06:00 CEST = 04:00 UTC
)
def test_window_start_on_dst_days(day: tuple[int, int, int], utc_hour: int) -> None:
    start = DAY.window_start(local(*day, 12, 0))
    assert start is not None
    assert start.astimezone(UTC) == datetime(*day, utc_hour, 0, tzinfo=UTC)


def tracker(started_at: datetime) -> SilenceTracker:
    return SilenceTracker(
        ["/RIG/KV6posinfo", "/GVB/KV6posinfo"],
        DAY,
        topic_limit=timedelta(minutes=15),
        restart_limit=timedelta(minutes=10),
        started_at=started_at,
    )


def test_no_false_alarm_after_a_quiet_night() -> None:
    t = tracker(local(2026, 10, 6, 23, 0))
    t.seen("/RIG/KV6posinfo", local(2026, 10, 6, 23, 50))
    t.seen("/GVB/KV6posinfo", local(2026, 10, 6, 23, 50))
    assert t.check(local(2026, 10, 7, 6, 5)) == []
    assert not t.all_silent(local(2026, 10, 7, 6, 5))
    assert t.check(local(2026, 10, 7, 6, 15)) == [
        TopicSilent("/RIG/KV6posinfo"),
        TopicSilent("/GVB/KV6posinfo"),
    ]


def test_one_silent_period_gives_one_start_and_one_end() -> None:
    start = local(2026, 10, 7, 12, 0)
    t = tracker(start)
    for minute in range(0, 31):
        t.seen("/RIG/KV6posinfo", start + timedelta(minutes=minute))
    events = []
    for minute in range(0, 31):
        events += t.check(start + timedelta(minutes=minute))
    assert events == [TopicSilent("/GVB/KV6posinfo")]
    assert t.seen("/GVB/KV6posinfo", start + timedelta(minutes=31)) == [
        TopicResumed("/GVB/KV6posinfo")
    ]
    assert t.check(start + timedelta(minutes=32)) == []


def test_all_silent_only_in_service_hours() -> None:
    t = tracker(local(2026, 10, 7, 12, 0))
    assert not t.all_silent(local(2026, 10, 7, 12, 9))
    assert t.all_silent(local(2026, 10, 7, 12, 10))

    night = tracker(local(2026, 10, 8, 0, 30))
    assert not night.all_silent(local(2026, 10, 8, 3, 0))


def test_any_topic_counts_against_all_silent() -> None:
    t = tracker(local(2026, 10, 7, 12, 0))
    t.seen("/QBUZZ/KV17cvlinfo", local(2026, 10, 7, 12, 8))
    assert not t.all_silent(local(2026, 10, 7, 12, 12))


def test_unknown_topic_is_ignored_by_tracker() -> None:
    t = tracker(local(2026, 10, 7, 12, 0))
    assert t.seen("/RIG/KV6posinfoExtra", local(2026, 10, 7, 12, 1)) == []
    assert t.check(local(2026, 10, 7, 12, 2)) == []
