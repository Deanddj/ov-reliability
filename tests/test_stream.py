import gzip
import time
from collections.abc import Callable
from datetime import UTC, datetime

import zmq
from conftest import FIXTURES

from ov_reliability.collector.stream import Connection, Event, Received, Tick, ZmqStream


def collect(stream: ZmqStream, until: Callable[[list[Event]], bool], timeout: float) -> list[Event]:
    events: list[Event] = []
    deadline = time.monotonic() + timeout
    for event in stream:
        events.append(event)
        if until(events) or time.monotonic() > deadline:
            break
    return events


def test_receives_subscribed_topics_and_reports_connection() -> None:
    payload = gzip.compress((FIXTURES / "kv6_rig.xml").read_bytes())
    context = zmq.Context()
    publisher = context.socket(zmq.XPUB)
    port = publisher.bind_to_random_port("tcp://127.0.0.1")
    stream = ZmqStream(
        f"tcp://127.0.0.1:{port}", ["/RIG/KV6posinfo"], lambda: datetime.now(UTC), poll_ms=100
    )
    try:
        # Wait for the subscription to arrive, otherwise the first messages are dropped.
        publisher.setsockopt(zmq.RCVTIMEO, 5000)
        assert publisher.recv() == b"\x01/RIG/KV6posinfo"
        publisher.send_multipart([b"/GVB/KV6posinfo", payload])
        publisher.send_multipart([b"/RIG/KV6posinfo", payload])

        events = collect(stream, lambda evs: any(isinstance(e, Received) for e in evs), timeout=5)
        received = [e for e in events if isinstance(e, Received)]
        assert [e.topic for e in received] == [b"/RIG/KV6posinfo"]
        assert b"".join(received[0].frames) == payload
        assert received[0].received_at.tzinfo is not None
        assert any(isinstance(e, Connection) and e.kind == "connected" for e in events)

        idle = collect(stream, lambda evs: any(isinstance(e, Tick) for e in evs), timeout=2)
        assert any(isinstance(e, Tick) for e in idle)
    finally:
        stream.close()
        publisher.close(linger=0)
        context.term()
