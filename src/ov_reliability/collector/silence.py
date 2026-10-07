"""Decides when topics count as silent, measured only within service hours."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")


@dataclass(frozen=True)
class ServiceHours:
    start: time
    end: time

    def window_start(self, now: datetime) -> datetime | None:
        """Start of the service window that contains `now`, or None outside service hours."""
        local = now.astimezone(AMSTERDAM)
        today_start = datetime.combine(local.date(), self.start, tzinfo=AMSTERDAM)
        clock = local.time()
        if self.start < self.end:
            return today_start if self.start <= clock < self.end else None
        # The window runs past midnight.
        if clock >= self.start:
            return today_start
        if clock < self.end:
            return today_start - timedelta(days=1)
        return None


@dataclass(frozen=True)
class TopicSilent:
    topic: str


@dataclass(frozen=True)
class TopicResumed:
    topic: str


type SilenceEvent = TopicSilent | TopicResumed


class SilenceTracker:
    def __init__(
        self,
        topics: Iterable[str],
        hours: ServiceHours,
        topic_limit: timedelta,
        restart_limit: timedelta,
        started_at: datetime,
    ) -> None:
        self._hours = hours
        self._topic_limit = topic_limit
        self._restart_limit = restart_limit
        self._last_seen = dict.fromkeys(topics, started_at)
        self._last_any = started_at
        self._silent: set[str] = set()

    def seen(self, topic: str, at: datetime) -> list[SilenceEvent]:
        self._last_any = at
        if topic not in self._last_seen:
            return []
        self._last_seen[topic] = at
        if topic in self._silent:
            self._silent.remove(topic)
            return [TopicResumed(topic)]
        return []

    def check(self, now: datetime) -> list[SilenceEvent]:
        window = self._hours.window_start(now)
        if window is None:
            return []
        events: list[SilenceEvent] = []
        for topic, last in self._last_seen.items():
            if topic not in self._silent and now - max(last, window) >= self._topic_limit:
                self._silent.add(topic)
                events.append(TopicSilent(topic))
        return events

    def all_silent(self, now: datetime) -> bool:
        window = self._hours.window_start(now)
        return window is not None and now - max(self._last_any, window) >= self._restart_limit
