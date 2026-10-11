"""Builders for small KV6 and KV17 messages in the shape the sources send."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str = "DEPARTURE"
    owner: str = "HTM"
    line: str = "3"
    day: str = "2026-10-09"
    journey: int = 30001
    reinforcement: int = 0
    stop: str = "8001"
    passage: int = 0
    timestamp: str | None = None  # 10:00 local on the operating day
    punctuality: str | None = "30"


def kv6_message(events: list[Event], prefix: str = "") -> str:
    p = f"{prefix}:" if prefix else ""
    xmlns = f"xmlns:{prefix}" if prefix else "xmlns"
    body = []
    for e in events:
        fields = [
            ("dataownercode", e.owner),
            ("lineplanningnumber", e.line),
            ("operatingday", e.day),
            ("journeynumber", str(e.journey)),
            ("reinforcementnumber", str(e.reinforcement)),
            ("userstopcode", e.stop),
            ("passagesequencenumber", str(e.passage)),
            ("timestamp", e.timestamp or f"{e.day}T10:00:00+02:00"),
            ("source", "SERVER"),
            ("vehiclenumber", "3001"),
        ]
        if e.punctuality is not None:
            fields.append(("punctuality", e.punctuality))
        inner = "".join(f"<{p}{name}>{value}</{p}{name}>" for name, value in fields)
        body.append(f"<{p}KV6posinfo><{p}{e.kind}>{inner}</{p}{e.kind}></{p}KV6posinfo>")
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<{p}VV_TM_PUSH {xmlns}="http://bison.connekt.nl/tmi8/kv6/msg">'
        f"<{p}DossierName>KV6posinfo</{p}DossierName>{''.join(body)}</{p}VV_TM_PUSH>"
    )


@dataclass(frozen=True)
class Journey:
    owner: str = "HTM"
    line: str = "3"
    day: str = "2026-10-09"
    journey: int = 30001
    reinforcement: int = 0


def kv17_message(
    journey: Journey,
    journey_mutations: list[tuple[str, str]] = [],  # noqa: B006
    stop_mutations: list[tuple[str, str, str, int]] = [],  # noqa: B006
) -> str:
    """journey_mutations: (timestamp, kind); stop_mutations: (timestamp, kind, stop, passage)."""
    key = (
        f"<KV17JOURNEY><dataownercode>{journey.owner}</dataownercode>"
        f"<lineplanningnumber>{journey.line}</lineplanningnumber>"
        f"<operatingday>{journey.day}</operatingday>"
        f"<journeynumber>{journey.journey}</journeynumber>"
        f"<reinforcementnumber>{journey.reinforcement}</reinforcementnumber></KV17JOURNEY>"
    )
    blocks = [
        f"<KV17MUTATEJOURNEY><timestamp>{at}</timestamp><{kind}/></KV17MUTATEJOURNEY>"
        for at, kind in journey_mutations
    ]
    blocks += [
        f"<KV17MUTATEJOURNEYSTOP><timestamp>{at}</timestamp><{kind}>"
        f"<userstopcode>{stop}</userstopcode><passagesequencenumber>{passage}"
        f"</passagesequencenumber></{kind}></KV17MUTATEJOURNEYSTOP>"
        for at, kind, stop, passage in stop_mutations
    ]
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<VV_TM_PUSH xmlns="http://bison.connekt.nl/tmi8/kv17/msg">'
        f"<KV17cvlinfo>{key}{''.join(blocks)}</KV17cvlinfo></VV_TM_PUSH>"
    )
