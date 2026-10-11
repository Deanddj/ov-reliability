import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from archived import archived
from hypothesis import given
from hypothesis import strategies as st
from kv6xml import Event, Journey, kv6_message, kv17_message

from ov_reliability.kv6.parse import (
    Parsed,
    TripKey,
    event_time,
    mentions_owner,
    parse_message,
)
from ov_reliability.kv6.payload import BadMessageError, decode_payload

RECEIVED = datetime(2026, 10, 9, 8, 0, 5, tzinfo=UTC)
TRIP = TripKey(date(2026, 10, 9), "HTM", "3", 30001, 0)


def kv6(events: list[Event], owners: tuple[str, ...] = ("HTM",), prefix: str = "") -> Parsed:
    root = ET.fromstring(kv6_message(events, prefix))
    return parse_message("/RIG/KV6posinfo", root, RECEIVED, owners)


def kv17(message: str, owners: tuple[str, ...] = ("HTM",)) -> Parsed:
    return parse_message("/RIG/KV17cvlinfo", ET.fromstring(message), RECEIVED, owners)


def test_departure_becomes_a_stop_event_and_a_sighting() -> None:
    parsed = kv6([Event()])
    assert parsed.bad == ()
    [event] = parsed.events
    assert event.trip == TRIP
    assert (event.stop_code, event.passage, event.event) == ("8001", 0, "DEPARTURE")
    assert event.event_at == datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
    assert event.punctuality == 30
    assert event.received_at == RECEIVED
    assert [s.trip for s in parsed.seen] == [TRIP]


def test_onstop_is_only_a_sighting() -> None:
    parsed = kv6([Event(kind="ONSTOP")])
    assert parsed.events == ()
    assert [s.trip for s in parsed.seen] == [TRIP]


@pytest.mark.parametrize("kind", ["INIT", "END", "DELAY", "ONROUTE", "OFFROUTE"])
def test_other_event_types_give_nothing(kind: str) -> None:
    assert kv6([Event(kind=kind, punctuality=None)]) == Parsed()


def test_other_owners_are_dropped() -> None:
    parsed = kv6([Event(owner="RET"), Event()])
    assert [e.trip.data_owner for e in parsed.events] == ["HTM"]


def test_namespace_prefix_does_not_matter() -> None:
    assert kv6([Event()], prefix="tmi8").events == kv6([Event()]).events


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (Event(kind="TELEPORT"), "unknown KV6 event 'TELEPORT'"),
        (Event(stop=""), "missing userstopcode"),
        (Event(punctuality="abc"), "punctuality is not an integer"),
        (Event(punctuality=None), "missing punctuality"),
        (Event(day="2026-13-01"), "bad operatingday"),
        (Event(timestamp="2026-10-09T10:06:00+02:00"), "after the message was received"),
        (Event(timestamp="2026-10-08T23:59:00+02:00"), "not on operating day 2026-10-09"),
    ],
)
def test_bad_event_is_reported_and_the_rest_kept(event: Event, reason: str) -> None:
    parsed = kv6([event, Event(journey=30002)])
    assert len(parsed.bad) == 1
    assert reason in parsed.bad[0]
    assert [e.trip.journey_number for e in parsed.events] == [30002]


def test_time_without_offset_is_amsterdam_time() -> None:
    assert event_time("2026-10-09T10:00:00", RECEIVED) == datetime(2026, 10, 9, 8, tzinfo=UTC)


def test_time_in_the_future_is_rejected() -> None:
    with pytest.raises(BadMessageError):
        event_time("2026-10-09T08:05:06Z", RECEIVED)
    assert event_time("2026-10-09T08:05:05Z", RECEIVED) == RECEIVED + timedelta(minutes=5)


def test_kv17_journey_and_stop_mutations() -> None:
    message = kv17_message(
        Journey(),
        journey_mutations=[("2026-10-09T09:59:00", "CANCEL")],
        stop_mutations=[("2026-10-09T09:59:30+02:00", "SHORTEN", "8002", 1)],
    )
    parsed = kv17(message)
    assert parsed.bad == ()
    cancel, shorten = parsed.mutations
    assert (cancel.trip, cancel.mutation, cancel.stop_code, cancel.passage) == (
        TRIP,
        "CANCEL",
        None,
        None,
    )
    assert cancel.mutated_at == datetime(2026, 10, 9, 7, 59, tzinfo=UTC)
    assert (shorten.mutation, shorten.stop_code, shorten.passage) == ("SHORTEN", "8002", 1)


def test_kv17_ignores_mutations_the_contract_does_not_use() -> None:
    message = kv17_message(
        Journey(), stop_mutations=[("2026-10-09T09:59:00+02:00", "LAG", "8002", 0)]
    )
    assert kv17(message) == Parsed()


def test_kv17_unknown_journey_mutation_is_bad() -> None:
    message = kv17_message(Journey(), journey_mutations=[("2026-10-09T09:59:00", "ADD")])
    parsed = kv17(message)
    assert parsed.mutations == ()
    assert "unknown journey mutation 'ADD'" in parsed.bad[0]


def test_kv17_of_other_owner_is_dropped() -> None:
    message = kv17_message(Journey(owner="GVB"), journey_mutations=[("2026-10-09T09:59", "CANCEL")])
    assert kv17(message) == Parsed()


def test_mentions_owner() -> None:
    message = kv6_message([Event(owner="RET")], prefix="tmi8")
    assert mentions_owner(message, ["HTM", "RET"])
    assert not mentions_owner(message, ["HTM"])


def test_other_topics_give_nothing() -> None:
    root = ET.fromstring(kv6_message([Event()]))
    assert parse_message("/RIG/KV15messages", root, RECEIVED, ["HTM"]) == Parsed()


LOCAL = ZoneInfo("Europe/Amsterdam")


@given(
    kind=st.sampled_from(["ARRIVAL", "DEPARTURE"]),
    owner=st.sampled_from(["HTM", "GVB", "RET", "CXX"]),
    line=st.from_regex(r"[A-Z]?[0-9]{1,4}", fullmatch=True),
    journey=st.integers(0, 9_999_999),
    reinforcement=st.integers(0, 9),
    stop=st.from_regex(r"[A-Z0-9]{1,10}", fullmatch=True),
    passage=st.integers(0, 9),
    punctuality=st.integers(-9999, 9999),
    at=st.datetimes(datetime(2026, 3, 1), datetime(2026, 11, 30)),
    with_offset=st.booleans(),
    prefix=st.sampled_from(["", "tmi8"]),
    lag=st.integers(-600, 3600),
    after_midnight=st.booleans(),
)
def test_parsed_event_keeps_every_field(
    kind: str,
    owner: str,
    line: str,
    journey: int,
    reinforcement: int,
    stop: str,
    passage: int,
    punctuality: int,
    at: datetime,
    with_offset: bool,
    prefix: str,
    lag: int,
    after_midnight: bool,
) -> None:
    moment = at.replace(tzinfo=LOCAL, fold=0)
    text = moment.isoformat() if with_offset else at.isoformat()
    received = moment.astimezone(UTC) + timedelta(seconds=lag)
    day = at.date() - timedelta(days=after_midnight)
    event = Event(kind, owner, line, day.isoformat(), journey, reinforcement, stop, passage, text,
                  str(punctuality))  # fmt: skip
    root = ET.fromstring(kv6_message([event], prefix))
    parsed = parse_message("/X/KV6posinfo", root, received, [owner])
    if lag < -300:
        assert parsed.events == ()
        assert len(parsed.bad) == 1
        return
    [stop_event] = parsed.events
    assert stop_event.trip == TripKey(day, owner, line, journey, reinforcement)
    assert (stop_event.stop_code, stop_event.passage, stop_event.event) == (stop, passage, kind)
    assert stop_event.punctuality == punctuality
    assert stop_event.event_at == moment.astimezone(UTC)
    assert stop_event.event_at <= stop_event.received_at + timedelta(minutes=5)


def parse_archived(name: str, owners: tuple[str, ...]) -> Parsed:
    record = archived(name)
    return parse_message(record.topic, decode_payload(record), record.received_at, owners)


def test_archived_htm_departure() -> None:
    [event] = parse_archived("kv6_htm_departure.jsonl", ("HTM",)).events
    assert event.trip == TripKey(date(2026, 10, 11), "HTM", "2", 28007, 0)
    assert (event.stop_code, event.event) == ("2882", "DEPARTURE")
    assert event.event_at == datetime(2026, 10, 11, 5, 36, 34, 760000, tzinfo=UTC)


def test_archived_cxx_message_with_two_blocks() -> None:
    parsed = parse_archived("kv17_cxx_two_blocks.jsonl", ("CXX",))
    assert [(m.trip.journey_number, m.mutation) for m in parsed.mutations] == [
        (46, "RECOVER"),
        (57, "RECOVER"),
    ]


def test_archived_gvb_cancel_without_offset() -> None:
    [cancel] = parse_archived("kv17_gvb_local_time.jsonl", ("GVB",)).mutations
    assert cancel.mutation == "CANCEL"
    assert cancel.mutated_at == datetime(2026, 10, 9, 6, 31, 6, tzinfo=UTC)


def test_archived_htm_shorten() -> None:
    parsed = parse_archived("kv17_htm_shorten.jsonl", ("HTM",))
    assert parsed.bad == ()
    assert {m.mutation for m in parsed.mutations} == {"SHORTEN"}
    assert parsed.mutations[0].stop_code == "2663"


def test_archived_arr_event_years_off_is_bad() -> None:
    parsed = parse_archived("kv6_arr_time_off.jsonl", ("ARR",))
    assert len(parsed.events) == 1
    [reason] = parsed.bad
    assert "2007-02-23" in reason


def test_archived_htm_recover() -> None:
    [recover] = parse_archived("kv17_htm_recover.jsonl", ("HTM",)).mutations
    assert (recover.trip.journey_number, recover.mutation) == (10200, "RECOVER")
