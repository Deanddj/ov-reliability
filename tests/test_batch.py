import base64
from datetime import UTC, date, datetime

from kv6xml import Event, Journey, kv6_message, kv17_message

from ov_reliability.collector.records import BadPayload, Marker, Message, Record
from ov_reliability.kv6.parse import TripKey
from ov_reliability.loader.batch import Batch, MarkerRow, SeenSpan, build_batch, window_start

RECEIVED = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)
TODAY = date(2026, 10, 9)
TRIP = TripKey(TODAY, "HTM", "3", 30001, 0)


def kv6(*events: Event, received: datetime = RECEIVED) -> Message:
    return Message(received, "/RIG/KV6posinfo", kv6_message(list(events)))


def build(records: list[Record], floor: date | None = None) -> Batch:
    return build_batch(records, ["HTM"], TODAY, 90, floor)


def test_events_sightings_and_mutations() -> None:
    cancel = kv17_message(
        Journey(journey=30002), journey_mutations=[("2026-10-09T10:20", "CANCEL")]
    )
    batch = build(
        [
            kv6(Event(kind="ARRIVAL", timestamp="2026-10-09T10:00:00+02:00")),
            kv6(Event(kind="ONSTOP", timestamp="2026-10-09T10:01:00+02:00")),
            kv6(Event(timestamp="2026-10-09T10:02:00+02:00")),
            Message(RECEIVED, "/RIG/KV17cvlinfo", cancel),
        ]
    )
    assert [e.event for e in batch.events] == ["ARRIVAL", "DEPARTURE"]
    assert batch.seen == (
        SeenSpan(
            TRIP, datetime(2026, 10, 9, 8, tzinfo=UTC), datetime(2026, 10, 9, 8, 2, tzinfo=UTC)
        ),
    )
    assert [(m.trip.journey_number, m.mutation) for m in batch.mutations] == [(30002, "CANCEL")]
    assert batch.bad == ()
    assert batch.days == {TODAY}


def test_markers_are_kept_and_other_topics_skipped() -> None:
    marker = Marker(RECEIVED, "started", {"run_id": "r1", "topics": ["/RIG/KV6posinfo"]})
    batch = build([marker, Message(RECEIVED, "/RIG/KV15messages", "<not even xml")])
    assert batch.markers == (MarkerRow(RECEIVED, "started", marker.details),)
    assert batch.bad == ()


def test_messages_without_a_loaded_owner_are_not_parsed() -> None:
    assert build([Message(RECEIVED, "/RIG/KV6posinfo", "<broken>RET<")]).bad == ()


def test_undecodable_payload_is_a_bad_message() -> None:
    record = BadPayload(RECEIVED, "/RIG/KV6posinfo", base64.b64encode(b"\xff>HTM<").decode(), "")
    [bad] = build([record]).bad
    assert (bad.received_at, bad.topic) == (RECEIVED, "/RIG/KV6posinfo")
    assert bad.reason.startswith("not XML")


def test_days_outside_the_window_are_bad_once_per_message() -> None:
    old = window_start(TODAY, 90).replace(day=1).isoformat()
    batch = build(
        [
            kv6(Event(day=old), Event(day=old, kind="ARRIVAL")),
            # A clock far ahead of the loader's "today" would deliver these.
            kv6(Event(day="2026-10-11"), received=datetime(2026, 10, 11, 9, tzinfo=UTC)),
            kv6(Event(day="2026-10-10"), received=datetime(2026, 10, 11, 9, tzinfo=UTC)),
        ]
    )
    assert [e.trip.operating_day for e in batch.events] == [date(2026, 10, 10)]
    assert [b.reason for b in batch.bad] == [
        f"operating day {old} is outside the window",
        "operating day 2026-10-11 is outside the window",
    ]


def test_days_before_the_floor_are_dropped_quietly() -> None:
    batch = build([kv6(Event(day="2026-10-07")), kv6(Event())], floor=date(2026, 10, 8))
    assert [e.trip.operating_day for e in batch.events] == [TODAY]
    assert batch.bad == ()


def test_window_start() -> None:
    assert window_start(TODAY, 90) == date(2026, 7, 11)
