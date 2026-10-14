import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

from archived import archived
from sirixml import Call, Vj, siri_message

from ov_reliability.kv6.parse import Parsed, TripKey
from ov_reliability.kv6.payload import decode_payload
from ov_reliability.siri.parse import parse_estimated_timetable

RECEIVED = datetime(2026, 10, 9, 8, 5, 30, tzinfo=UTC)
TRIP = TripKey(date(2026, 10, 9), "GVB", "50", 79567, 0)


def parse(*journeys: Vj, received: datetime = RECEIVED) -> Parsed:
    root = ET.fromstring(siri_message(list(journeys)))
    return parse_estimated_timetable(root, received, ["GVB"])


def parse_archived(name: str) -> Parsed:
    record = archived(name)
    return parse_estimated_timetable(decode_payload(record), record.received_at, ["GVB"])


def test_a_recorded_call_gives_an_arrival_and_a_departure() -> None:
    parsed = parse(Vj())
    assert [(e.trip, e.stop_code, e.passage, e.event) for e in parsed.events] == [
        (TRIP, "03205", 0, "ARRIVAL"),
        (TRIP, "03205", 0, "DEPARTURE"),
    ]
    at = datetime(2026, 10, 9, 8, 1, tzinfo=UTC)
    assert {(e.event_at, e.punctuality, e.received_at) for e in parsed.events} == {
        (at, 60, RECEIVED)
    }
    assert {s.at for s in parsed.seen} == {at}
    assert parsed.mutations == ()
    assert parsed.bad == ()


def test_calls_without_an_actual_time_are_not_events() -> None:
    parsed = parse(Vj(recorded=(Call(actual=None),), estimated=(Call(stop="03203"),)))
    assert parsed == Parsed()


def test_other_owners_are_skipped() -> None:
    assert parse(Vj(owner="RET")) == Parsed()


def test_cancellation_before_the_last_stop_counts() -> None:
    journey = Vj(
        cancelled=True,
        recorded=(),
        estimated=(
            Call(stop="03205", aimed="2026-10-09T10:20:00+02:00", cancelled=True),
            Call(stop="03203", aimed="2026-10-09T10:22:00+02:00"),
        ),
    )
    parsed = parse(journey)
    at = datetime(2026, 10, 9, 8, 5, tzinfo=UTC)
    assert [(m.mutation, m.stop_code, m.passage, m.mutated_at) for m in parsed.mutations] == [
        ("CANCEL", None, None, at),
        ("SHORTEN", "03205", 0, at),
    ]
    late = datetime(2026, 10, 9, 8, 22, tzinfo=UTC)
    assert parse(journey, received=late).mutations == ()


def test_broken_journeys_are_bad_and_the_rest_is_kept() -> None:
    parsed = parse(
        Vj(journey="1", recorded=(Call(), Call())),
        Vj(journey="2", recorded=(Call(aimed=None),)),
        Vj(journey="3", recorded=(Call(actual="2026-10-09T10:30:00+02:00"),)),
        Vj(journey="4", cancelled=True, recorded=(Call(aimed=None, actual=None),)),
        Vj(journey="GVB:x"),
        Vj(),
    )
    assert {e.trip for e in parsed.events} == {TRIP}
    assert parsed.bad == (
        "SIRI: journey 1 names a stop twice",
        "SIRI: ActualArrivalTime without AimedArrivalTime at stop 03205",
        "SIRI: time '2026-10-09T10:30:00+02:00' is after the message was received",
        "SIRI: journey 4 is cancelled without times",
        "SIRI: bad DatedVehicleJourneyRef 'GVB:VehicleJourney:GVB:x'",
    )


def test_archived_journey_with_recorded_calls() -> None:
    parsed = parse_archived("siri_gvb_recorded.jsonl")
    assert parsed.bad == ()
    assert parsed.mutations == ()
    trip = TripKey(date(2026, 10, 12), "GVB", "50", 79567, 0)
    assert {e.trip for e in parsed.events} == {trip}
    assert len(parsed.events) == 8
    # Aimed 09:53:55, actual 09:55:10 local.
    first = parsed.events[1]
    assert (first.stop_code, first.event, first.punctuality) == ("03205", "DEPARTURE", 75)
    assert first.event_at == datetime(2026, 10, 12, 7, 55, 10, tzinfo=UTC)


def test_archived_resend_after_the_day_is_not_a_cancellation() -> None:
    assert parse_archived("siri_gvb_resend.jsonl") == Parsed()


def test_archived_cancellation() -> None:
    parsed = parse_archived("siri_gvb_cancel.jsonl")
    assert parsed.bad == ()
    kinds = [m.mutation for m in parsed.mutations]
    assert kinds[0] == "CANCEL"
    assert set(kinds[1:]) == {"SHORTEN"}
    assert parsed.mutations[0].trip == TripKey(date(2026, 10, 8), "GVB", "54", 102498, 0)
