"""The recorder loop: stream events in, archive records out."""

import base64
import gzip
import logging
import shutil
import uuid
import zlib
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, assert_never

from ov_reliability.collector.archive import ArchiveWriter, apply_retention, hour_of
from ov_reliability.collector.records import RECORDER_TOPIC, BadPayload, Marker, Message, Record
from ov_reliability.collector.silence import (
    ServiceHours,
    SilenceEvent,
    SilenceTracker,
    TopicResumed,
    TopicSilent,
)
from ov_reliability.collector.stream import Connection, Event, Received, Tick
from ov_reliability.config import Config

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_SILENCE = 1
_UNSEEN_CHECK_AFTER = timedelta(hours=1)


def to_record(event: Received) -> Message | BadPayload:
    raw = b"".join(event.frames)
    try:
        topic = event.topic.decode("utf-8")
    except UnicodeDecodeError:
        return BadPayload(
            event.received_at,
            event.topic.decode("utf-8", errors="replace"),
            _b64(raw),
            f"topic is not valid UTF-8: {event.topic!r}",
        )
    try:
        payload = gzip.decompress(raw).decode("utf-8")
    except (OSError, EOFError, zlib.error, UnicodeDecodeError) as exc:
        return BadPayload(event.received_at, topic, _b64(raw), f"{type(exc).__name__}: {exc}")
    return Message(event.received_at, topic, payload)


class Recorder:
    def __init__(
        self,
        config: Config,
        writer: ArchiveWriter,
        clock: Callable[[], datetime],
        should_stop: Callable[[], bool],
        version: str,
        run_id: str | None = None,
    ) -> None:
        self._config = config
        self._writer = writer
        self._clock = clock
        self._should_stop = should_stop
        self._version = version
        self._run_id = run_id or str(uuid.uuid4())
        self._seen_topics: set[str] = set()
        self._counts: Counter[str] = Counter()
        self._bad_payloads = 0
        self._bytes = 0

    def run(self, events: Iterable[Event]) -> int:
        started_at = self._clock()
        monitoring = self._config.monitoring
        self._silence = SilenceTracker(
            self._config.kv6_topics,
            ServiceHours(*monitoring.service_hours),
            timedelta(minutes=monitoring.topic_silence_minutes),
            timedelta(minutes=monitoring.restart_silence_minutes),
            started_at,
        )
        self._hour = hour_of(started_at)
        unseen_checked = False
        apply_retention(
            self._config.archive.directory, started_at, self._config.archive.retention_days
        )
        self._marker(
            started_at,
            "started",
            version=self._version,
            endpoint=self._config.source.endpoint,
            topics=list(self._config.source.topics),
        )
        log.info("Recording %d topics, run %s", len(self._config.source.topics), self._run_id)
        try:
            for event in events:
                now = _event_time(event)
                # Roll over before handling, so the first record of a new hour is
                # not counted in the previous hour's health line.
                if hour_of(now) != self._hour:
                    self._roll_over(now)
                self._handle(event)
                self._writer.maintain(now)
                if not unseen_checked and now - started_at >= _UNSEEN_CHECK_AFTER:
                    self._report_unseen_topics()
                    unseen_checked = True
                for silence in self._silence.check(now):
                    self._on_silence(silence, now)
                if self._silence.all_silent(now):
                    log.error(
                        "No messages on any topic for %d minutes during service hours; "
                        "exiting so the connection is rebuilt",
                        monitoring.restart_silence_minutes,
                    )
                    self._stop(now, "silence")
                    return EXIT_SILENCE
                if self._should_stop():
                    self._stop(now, "signal")
                    return EXIT_OK
        finally:
            self._writer.close()
        raise RuntimeError("event stream ended without a stop request")

    def _handle(self, event: Event) -> None:
        match event:
            case Received():
                self._record(to_record(event))
            case Connection():
                log.info("ZeroMQ %s (%s)", event.kind, self._config.source.endpoint)
                self._marker(event.at, event.kind, endpoint=self._config.source.endpoint)
            case Tick():
                pass
            case _:
                assert_never(event)

    def _record(self, record: Message | BadPayload) -> None:
        if isinstance(record, BadPayload):
            self._bad_payloads += 1
            log.warning("Bad payload on %s: %s", record.topic, record.error)
        elif record.topic not in self._seen_topics:
            self._seen_topics.add(record.topic)
            log.info("First message on %s", record.topic)
        self._write(record)
        for silence in self._silence.seen(record.topic, record.received_at):
            self._on_silence(silence, record.received_at)

    def _on_silence(self, event: SilenceEvent, at: datetime) -> None:
        match event:
            case TopicSilent(topic):
                log.warning(
                    "No messages on %s for %d minutes",
                    topic,
                    self._config.monitoring.topic_silence_minutes,
                )
                self._marker(at, "silence_start", silent_topic=topic)
            case TopicResumed(topic):
                log.info("Messages on %s again", topic)
                self._marker(at, "silence_end", silent_topic=topic)
            case _:
                assert_never(event)

    def _roll_over(self, now: datetime) -> None:
        free_gb = shutil.disk_usage(self._config.archive.directory).free / 1e9
        per_topic = ", ".join(f"{topic}={n}" for topic, n in sorted(self._counts.items()))
        low_disk = free_gb < self._config.archive.disk_warning_gb
        log.log(
            logging.WARNING if low_disk else logging.INFO,
            "%s: %d records (%s), %d bad payloads, %.1f MB written, %.1f GB free%s",
            f"{self._hour:%Y-%m-%dT%H}Z",
            self._counts.total(),
            per_topic or "none",
            self._bad_payloads,
            self._bytes / 1e6,
            free_gb,
            f"; low free disk, warning below {self._config.archive.disk_warning_gb:.0f} GB"
            if low_disk
            else "",
        )
        apply_retention(self._config.archive.directory, now, self._config.archive.retention_days)
        self._counts.clear()
        self._bad_payloads = 0
        self._bytes = 0
        self._hour = hour_of(now)

    def _report_unseen_topics(self) -> None:
        unseen = [t for t in self._config.source.topics if t not in self._seen_topics]
        if unseen:
            log.warning("Subscribed topics not seen in the first hour: %s", ", ".join(unseen))

    def _stop(self, at: datetime, reason: str) -> None:
        self._marker(at, "stopped", reason=reason)
        self._writer.close()
        log.info("Stopped (%s), run %s", reason, self._run_id)

    def _marker(self, at: datetime, event: str, **details: Any) -> None:
        self._write(Marker(at, event, {"run_id": self._run_id, **details}))

    def _write(self, record: Record) -> None:
        topic = record.topic if isinstance(record, Message | BadPayload) else RECORDER_TOPIC
        self._counts[topic] += 1
        self._bytes += self._writer.write(record)


def _event_time(event: Event) -> datetime:
    match event:
        case Received():
            return event.received_at
        case Connection():
            return event.at
        case Tick():
            return event.now
        case _:
            assert_never(event)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")
