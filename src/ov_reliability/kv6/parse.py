"""KV6 stop events and KV17 mutations from a decoded message (decisions 067, 068, 072, 076)."""

import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from ov_reliability.kv6.payload import BadMessageError

LOCAL = ZoneInfo("Europe/Amsterdam")
# An event can't happen after we received it; the margin covers clock differences.
MAX_AHEAD = timedelta(minutes=5)

type StopEventType = Literal["ARRIVAL", "DEPARTURE"]
type MutationType = Literal["CANCEL", "RECOVER", "NOTMONITORED", "SHORTEN"]

_STOP_EVENTS: frozenset[str] = frozenset({"ARRIVAL", "DEPARTURE"})
_SEEN_EVENTS = frozenset({"ARRIVAL", "ONSTOP", "DEPARTURE"})
_OTHER_EVENTS = frozenset({"INIT", "END", "DELAY", "ONROUTE", "OFFROUTE"})
_STOP_MUTATIONS = frozenset({"SHORTEN"})
_IGNORED_STOP_MUTATIONS = frozenset(
    {"LAG", "CHANGEPASSTIMES", "CHANGEDESTINATION", "MUTATIONMESSAGE"}
)


@dataclass(frozen=True, order=True)
class TripKey:
    operating_day: date
    data_owner: str
    planning_number: str
    journey_number: int
    reinforcement: int


@dataclass(frozen=True)
class StopEvent:
    trip: TripKey
    stop_code: str
    passage: int
    event: StopEventType
    event_at: datetime
    punctuality: int
    received_at: datetime


@dataclass(frozen=True)
class TripSeen:
    trip: TripKey
    at: datetime


@dataclass(frozen=True)
class Mutation:
    trip: TripKey
    mutation: MutationType
    stop_code: str | None
    passage: int | None
    mutated_at: datetime
    received_at: datetime


@dataclass(frozen=True)
class Parsed:
    events: tuple[StopEvent, ...] = ()
    seen: tuple[TripSeen, ...] = ()
    mutations: tuple[Mutation, ...] = ()
    bad: tuple[str, ...] = ()


def mentions_owner(payload: str, owners: Collection[str]) -> bool:
    """Cheap test before parsing: every event names its owner as element text."""
    return any(f">{owner}<" in payload for owner in owners)


def parse_message(
    topic: str, root: ET.Element, received_at: datetime, owners: Collection[str]
) -> Parsed:
    if topic.endswith("/KV6posinfo"):
        return _parse_kv6(root, received_at, owners)
    if topic.endswith("/KV17cvlinfo"):
        return _parse_kv17(root, received_at, owners)
    return Parsed()


def event_time(text: str, received_at: datetime) -> datetime:
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        raise BadMessageError(f"bad time {text!r}") from None
    if at.tzinfo is None:
        at = at.replace(tzinfo=LOCAL)
    at = at.astimezone(UTC)
    if at > received_at + MAX_AHEAD:
        raise BadMessageError(f"time {text!r} is after the message was received")
    return at


def _parse_kv6(root: ET.Element, received_at: datetime, owners: Collection[str]) -> Parsed:
    events: list[StopEvent] = []
    seen: list[TripSeen] = []
    bad: list[str] = []
    for info in root.iterfind("{*}KV6posinfo"):
        for element in info:
            kind = _local(element.tag)
            if kind in _OTHER_EVENTS:
                continue
            if kind not in _SEEN_EVENTS:
                bad.append(f"unknown KV6 event {kind!r}")
                continue
            fields = _fields(element)
            if fields.get("dataownercode") not in owners:
                continue
            try:
                trip = _trip(fields)
                at = event_time(_required(fields, "timestamp"), received_at)
                _check_on_operating_day(trip, at)
                if kind in _STOP_EVENTS:
                    events.append(
                        StopEvent(
                            trip,
                            _required(fields, "userstopcode"),
                            _integer(fields, "passagesequencenumber"),
                            "ARRIVAL" if kind == "ARRIVAL" else "DEPARTURE",
                            at,
                            _integer(fields, "punctuality"),
                            received_at,
                        )
                    )
                seen.append(TripSeen(trip, at))
            except BadMessageError as exc:
                bad.append(f"KV6 {kind}: {exc}")
    return Parsed(tuple(events), tuple(seen), (), tuple(bad))


def _check_on_operating_day(trip: TripKey, at: datetime) -> None:
    # A trip runs on its operating day or, after midnight, the next one. ARR has sent events
    # dated years back; none of the loaded owners ever left this range (findings 2026-10-11).
    local_day = at.astimezone(LOCAL).date()
    if not trip.operating_day <= local_day <= trip.operating_day + timedelta(days=1):
        raise BadMessageError(f"time {at.isoformat()} is not on operating day {trip.operating_day}")


def _parse_kv17(root: ET.Element, received_at: datetime, owners: Collection[str]) -> Parsed:
    mutations: list[Mutation] = []
    bad: list[str] = []
    for info in root.iterfind("{*}KV17cvlinfo"):
        journey = info.find("{*}KV17JOURNEY")
        if journey is None:
            bad.append("KV17cvlinfo without KV17JOURNEY")
            continue
        fields = _fields(journey)
        if fields.get("dataownercode") not in owners:
            continue
        try:
            trip = _trip(fields)
            for block in info:
                level = _local(block.tag)
                if level == "KV17MUTATEJOURNEY":
                    mutations.extend(_journey_mutations(trip, block, received_at))
                elif level == "KV17MUTATEJOURNEYSTOP":
                    mutations.extend(_stop_mutations(trip, block, received_at))
        except BadMessageError as exc:
            bad.append(f"KV17: {exc}")
    return Parsed((), (), tuple(mutations), tuple(bad))


def _journey_mutations(trip: TripKey, block: ET.Element, received_at: datetime) -> list[Mutation]:
    at = _block_time(block, received_at)
    found: list[Mutation] = []
    for element in block:
        kind = _local(element.tag)
        if kind == "timestamp":
            continue
        found.append(Mutation(trip, _mutation(kind), None, None, at, received_at))
    return found


def _stop_mutations(trip: TripKey, block: ET.Element, received_at: datetime) -> list[Mutation]:
    found: list[Mutation] = []
    elements = [element for element in block if _local(element.tag) != "timestamp"]
    if not elements:
        return found
    at = _block_time(block, received_at)
    for element in elements:
        kind = _local(element.tag)
        if kind in _IGNORED_STOP_MUTATIONS:
            continue
        if kind not in _STOP_MUTATIONS:
            raise BadMessageError(f"unknown stop mutation {kind!r}")
        fields = _fields(element)
        found.append(
            Mutation(
                trip,
                "SHORTEN",
                _required(fields, "userstopcode"),
                _integer(fields, "passagesequencenumber"),
                at,
                received_at,
            )
        )
    return found


def _block_time(block: ET.Element, received_at: datetime) -> datetime:
    text = block.findtext("{*}timestamp")
    if text is None:
        raise BadMessageError("mutation without timestamp")
    return event_time(text, received_at)


def _mutation(kind: str) -> MutationType:
    match kind:
        case "CANCEL":
            return "CANCEL"
        case "RECOVER":
            return "RECOVER"
        case "NOTMONITORED":
            return "NOTMONITORED"
    raise BadMessageError(f"unknown journey mutation {kind!r}")


def _trip(fields: dict[str, str]) -> TripKey:
    day_text = _required(fields, "operatingday")
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        raise BadMessageError(f"bad operatingday {day_text!r}") from None
    return TripKey(
        day,
        _required(fields, "dataownercode"),
        _required(fields, "lineplanningnumber"),
        _integer(fields, "journeynumber"),
        _integer(fields, "reinforcementnumber"),
    )


def _fields(element: ET.Element) -> dict[str, str]:
    return {_local(child.tag): (child.text or "").strip() for child in element}


def _required(fields: dict[str, str], name: str) -> str:
    value = fields.get(name)
    if not value:
        raise BadMessageError(f"missing {name}")
    return value


def _integer(fields: dict[str, str], name: str) -> int:
    value = _required(fields, name)
    try:
        return int(value)
    except ValueError:
        raise BadMessageError(f"{name} is not an integer: {value!r}") from None


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]
