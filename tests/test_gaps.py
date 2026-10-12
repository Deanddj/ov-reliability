from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ov_reliability.collector.records import Marker
from ov_reliability.stats.gaps import Gap, gaps

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
KV6 = "/RIG/KV6posinfo"
KV17 = "/RIG/KV17cvlinfo"
TOPICS = (KV6, KV17)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def marker(minutes: float, event: str, **details: Any) -> Marker:
    return Marker(at(minutes), event, details)


def started(minutes: float, topics: Sequence[str] = ("/RIG/",)) -> Marker:
    return marker(minutes, "started", topics=list(topics))


def gap(start: float, end: float, reason: str, topic: str | None = None) -> Gap:
    return Gap("collector", topic, at(start), at(end), reason)


def test_no_markers_is_one_gap() -> None:
    assert gaps("collector", [], TOPICS, at(0), at(60)) == [gap(0, 60, "not_started")]


def test_running_the_whole_time_is_no_gap() -> None:
    assert gaps("collector", [started(-5)], TOPICS, at(0), at(60)) == []


def test_before_the_first_start() -> None:
    assert gaps("collector", [started(10)], TOPICS, at(0), at(60)) == [gap(0, 10, "not_started")]


def test_stopped_until_started() -> None:
    markers = [started(-5), marker(20, "stopped", reason="signal"), started(21)]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == [gap(20, 21, "stopped")]


def test_open_gap_ends_at_until() -> None:
    markers = [started(-5), marker(20, "stopped", reason="signal")]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == [gap(20, 60, "stopped")]


def test_disconnected_until_connected() -> None:
    markers = [started(-5), marker(20, "disconnected"), marker(25, "connected")]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == [gap(20, 25, "disconnected")]


def test_stopped_while_disconnected() -> None:
    markers = [started(-5), marker(20, "disconnected"), marker(25, "stopped"), started(30)]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == [
        gap(20, 25, "disconnected"),
        gap(25, 30, "stopped"),
    ]


def test_silence_of_a_requested_topic() -> None:
    markers = [
        started(-5),
        marker(20, "silence_start", silent_topic=KV6),
        marker(22, "silence_start", silent_topic="/GVB/KV6posinfo"),
        marker(35, "silence_end", silent_topic=KV6),
    ]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == [gap(20, 35, "silence", KV6)]


def test_a_restart_ends_a_silence() -> None:
    markers = [started(-5), marker(20, "silence_start", silent_topic=KV6), started(40)]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == [gap(20, 40, "silence", KV6)]


def test_topic_not_subscribed_in_a_run() -> None:
    # Subscriptions are prefixes, so "/RIG/KV6" records "/RIG/KV6posinfo".
    markers = [
        started(-5, ["/RIG/KV6"]),
        marker(30, "stopped"),
        started(31, ["/RIG/KV6", "/RIG/KV17cvlinfo"]),
    ]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == [
        gap(0, 30, "not_subscribed", KV17),
        gap(30, 31, "stopped"),
    ]


def test_gaps_are_cut_to_the_range() -> None:
    markers = [started(-50), marker(-30, "stopped"), started(10), marker(50, "disconnected")]
    assert gaps("collector", markers, TOPICS, at(0), at(40)) == [gap(0, 10, "stopped")]


def test_markers_at_one_moment() -> None:
    # The loader returns markers in key order, where "connected" sorts before "started".
    markers = [
        marker(0, "connected"),
        started(0),
        started(20),
        marker(20, "stopped"),
        marker(30, "connected"),
        marker(30, "disconnected"),
    ]
    assert gaps("collector", markers, TOPICS, at(0), at(60)) == []


def test_unknown_marker_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown marker 'paused'"):
        gaps("collector", [started(-5), marker(5, "paused")], TOPICS, at(0), at(60))


def test_started_without_topics_is_an_error() -> None:
    with pytest.raises(ValueError, match="no list of topics"):
        gaps("collector", [marker(5, "started")], TOPICS, at(0), at(60))


EVENTS = ("stopped", "started", "disconnected", "connected", "silence_start", "silence_end")
SUBSCRIPTIONS = [["/RIG/"], ["/RIG/KV6"], ["/GVB/"], ["/RIG/KV6", "/RIG/KV17"]]


@st.composite
def marker_lists(draw: st.DrawFn) -> list[Marker]:
    markers = []
    for _ in range(draw(st.integers(0, 12))):
        minutes = draw(st.integers(-30, 90))
        event = draw(st.sampled_from(EVENTS))
        if event == "started":
            markers.append(started(minutes, draw(st.sampled_from(SUBSCRIPTIONS))))
        elif event.startswith("silence"):
            topic = draw(st.sampled_from([KV6, KV17, "/GVB/KV6posinfo"]))
            markers.append(marker(minutes, event, silent_topic=topic))
        else:
            markers.append(marker(minutes, event))
    return markers


def recording(markers: list[Marker], topic: str, moment: datetime) -> bool:
    """Replays the markers up to `moment`, one at a time, the simple way."""
    order = {event: rank for rank, event in enumerate(EVENTS)}
    running, disconnected = False, False
    subscribed: list[str] = []
    silent: set[str] = set()
    for item in sorted(markers, key=lambda m: (m.received_at, order[m.event])):
        if item.received_at > moment:
            break
        if item.event == "started":
            running, disconnected, silent = True, False, set()
            subscribed = item.details["topics"]
        elif item.event == "stopped" and running:
            running = False
        elif item.event == "disconnected" and running:
            disconnected = True
        elif item.event == "connected":
            disconnected = False
        elif item.event == "silence_start" and running:
            silent.add(item.details["silent_topic"])
        elif item.event == "silence_end":
            silent.discard(item.details["silent_topic"])
    return (
        running
        and not disconnected
        and topic not in silent
        and any(topic.startswith(prefix) for prefix in subscribed)
    )


@given(marker_lists(), st.integers(-10, 40), st.integers(0, 80))
def test_a_moment_is_in_a_gap_exactly_when_not_recording(
    markers: list[Marker], start_minute: int, length: int
) -> None:
    start, until = at(start_minute), at(start_minute + length)
    found = gaps("collector", markers, TOPICS, start, until)
    for item in found:
        assert start <= item.start < item.end <= until
    for topic in (*TOPICS, None):
        spans = sorted((g.start, g.end) for g in found if g.topic == topic)
        assert all(end <= next_start for (_, end), (next_start, _) in pairwise(spans))
    moments = {start + timedelta(seconds=30 * i) for i in range(2 * length)}
    moments |= {m.received_at for m in markers if start <= m.received_at < until}
    for moment in moments:
        for topic in TOPICS:
            in_gap = any(g.start <= moment < g.end and g.topic in (None, topic) for g in found)
            assert in_gap == (not recording(markers, topic, moment))
