"""Builder for small SIRI EstimatedTimetable messages in the shape GVB sends."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Call:
    stop: str = "03205"
    aimed: str | None = "2026-10-09T10:00:00+02:00"
    actual: str | None = "2026-10-09T10:01:00+02:00"
    cancelled: bool = False


@dataclass(frozen=True)
class Vj:
    owner: str = "GVB"
    line: str = "50"
    day: str = "20261009"
    journey: str = "79567"
    cancelled: bool = False
    recorded_at: str = "2026-10-09T10:05:00+02:00"
    recorded: tuple[Call, ...] = (Call(),)
    estimated: tuple[Call, ...] = field(default_factory=tuple)


def _call(tag: str, call: Call) -> str:
    parts = [
        f"<siri:StopPointRef>GVB:StopPoint:{call.stop}</siri:StopPointRef>",
        f"<siri:Cancellation>{str(call.cancelled).lower()}</siri:Cancellation>",
    ]
    for name in ("Arrival", "Departure"):
        if call.aimed is not None:
            parts.append(f"<siri:Aimed{name}Time>{call.aimed}</siri:Aimed{name}Time>")
        if call.actual is not None and tag == "RecordedCall":
            parts.append(f"<siri:Actual{name}Time>{call.actual}</siri:Actual{name}Time>")
    return f"<siri:{tag}>{''.join(parts)}</siri:{tag}>"


def siri_message(journeys: list[Vj]) -> str:
    body = []
    for vj in journeys:
        recorded = "".join(_call("RecordedCall", call) for call in vj.recorded)
        estimated = "".join(_call("EstimatedCall", call) for call in vj.estimated)
        body.append(
            "<siri:EstimatedVehicleJourney>"
            f"<siri:RecordedAtTime>{vj.recorded_at}</siri:RecordedAtTime>"
            f"<siri:LineRef>{vj.owner}:Line:{vj.line}</siri:LineRef>"
            "<siri:FramedVehicleJourneyRef>"
            f"<siri:DataFrameRef>{vj.day}</siri:DataFrameRef>"
            "<siri:DatedVehicleJourneyRef>"
            f"{vj.owner}:VehicleJourney:{vj.journey}"
            "</siri:DatedVehicleJourneyRef>"
            "</siri:FramedVehicleJourneyRef>"
            f"<siri:Cancellation>{str(vj.cancelled).lower()}</siri:Cancellation>"
            f"<siri:RecordedCalls>{recorded}</siri:RecordedCalls>"
            f"<siri:EstimatedCalls>{estimated}</siri:EstimatedCalls>"
            "</siri:EstimatedVehicleJourney>"
        )
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:siriWS="http://wsdl.siri.org.uk" xmlns:siri="http://www.siri.org.uk/siri">'
        "<soap:Body><siriWS:NotifyEstimatedTimetable><Notification>"
        "<siri:EstimatedTimetableDelivery><siri:EstimatedJourneyVersionFrame>"
        f"{''.join(body)}"
        "</siri:EstimatedJourneyVersionFrame></siri:EstimatedTimetableDelivery>"
        "</Notification></siriWS:NotifyEstimatedTimetable></soap:Body></soap:Envelope>"
    )
