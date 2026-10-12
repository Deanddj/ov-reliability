"""Recorder gaps: when a recorder was not recording a topic, derived from its markers."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from ov_reliability.collector.records import Marker

# Markers that share a moment are applied in this order, so a restart within one millisecond
# still closes the gap it opened.
_ORDER = {
    event: rank
    for rank, event in enumerate(
        ("stopped", "started", "disconnected", "connected", "silence_start", "silence_end")
    )
}


@dataclass(frozen=True)
class Gap:
    recorder: str
    # None when the whole recorder was not recording.
    topic: str | None
    start: datetime
    end: datetime
    reason: str


def gaps(
    recorder: str,
    markers: Iterable[Marker],
    topics: Collection[str],
    start: datetime,
    until: datetime,
) -> list[Gap]:
    """The gaps between `start` and `until` for the given topics (decision 070).

    `markers` must include the recorder's last `started` at or before `start`, if there is one;
    without it, everything before the first `started` counts as not recording.
    """
    found: list[Gap] = []
    opened: dict[tuple[str | None, str], datetime] = {}
    running = False

    def open_gap(topic: str | None, reason: str, at: datetime) -> None:
        opened.setdefault((topic, reason), at)

    def close(at: datetime, topic: str | None = None, reason: str | None = None) -> None:
        for key in [key for key in opened if reason is None or key == (topic, reason)]:
            since = opened.pop(key)
            gap_start, gap_end = max(since, start), min(at, until)
            if gap_start < gap_end:
                found.append(Gap(recorder, key[0], gap_start, gap_end, key[1]))

    open_gap(None, "not_started", start)
    for marker in sorted(markers, key=_marker_order):
        at = marker.received_at
        if at > until:
            break
        match marker.event:
            case "started":
                close(at)
                running = True
                subscribed = _subscribed_prefixes(marker)
                for topic in topics:
                    if not topic.startswith(subscribed):
                        open_gap(topic, "not_subscribed", at)
            case "stopped" if running:
                close(at)
                running = False
                open_gap(None, "stopped", at)
            case "disconnected" if running:
                open_gap(None, "disconnected", at)
            case "connected":
                close(at, None, "disconnected")
            case "silence_start" if running and marker.details.get("silent_topic") in topics:
                topic = marker.details["silent_topic"]
                if (topic, "not_subscribed") not in opened:
                    open_gap(topic, "silence", at)
            case "silence_end":
                close(at, marker.details.get("silent_topic"), "silence")
            case "stopped" | "disconnected" | "silence_start":
                pass
            case _:
                raise ValueError(f"unknown marker {marker.event!r} of {recorder} at {at}")
    close(until)
    return sorted(found, key=lambda gap: (gap.start, gap.topic or "", gap.reason))


def _marker_order(marker: Marker) -> tuple[datetime, int]:
    return marker.received_at, _ORDER.get(marker.event, len(_ORDER))


def _subscribed_prefixes(marker: Marker) -> tuple[str, ...]:
    topics = marker.details.get("topics")
    if not isinstance(topics, list) or not all(isinstance(topic, str) for topic in topics):
        raise ValueError(f"started marker at {marker.received_at} has no list of topics")
    # Subscriptions match by prefix: "/KEOLIS/KV15message" records "/KEOLIS/KV15messages".
    return tuple(topics)
