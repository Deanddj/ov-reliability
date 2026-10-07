"""Turns the NDOV Loket ZeroMQ subscription into a stream of events."""

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import zmq
from zmq.utils.monitor import recv_monitor_message

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Received:
    received_at: datetime
    topic: bytes
    frames: tuple[bytes, ...]


@dataclass(frozen=True)
class Connection:
    kind: Literal["connected", "disconnected"]
    at: datetime


@dataclass(frozen=True)
class Tick:
    now: datetime


type Event = Received | Connection | Tick


class ZmqStream:
    def __init__(
        self,
        endpoint: str,
        topics: Iterable[str],
        clock: Callable[[], datetime],
        poll_ms: int = 1000,
    ) -> None:
        self._clock = clock
        self._poll_ms = poll_ms
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.SUB)
        # Without keepalive a connection that dies silently is never noticed.
        self._socket.setsockopt(zmq.TCP_KEEPALIVE, 1)
        self._socket.setsockopt(zmq.TCP_KEEPALIVE_IDLE, 60)
        self._socket.setsockopt(zmq.TCP_KEEPALIVE_INTVL, 10)
        self._socket.setsockopt(zmq.RECONNECT_IVL_MAX, 30_000)
        self._socket.setsockopt(zmq.RCVHWM, 100_000)
        self._monitor = self._socket.get_monitor_socket(
            zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED
        )
        for topic in topics:
            self._socket.setsockopt(zmq.SUBSCRIBE, topic.encode())
        self._socket.connect(endpoint)
        self._poller = zmq.Poller()
        self._poller.register(self._socket, zmq.POLLIN)
        self._poller.register(self._monitor, zmq.POLLIN)

    def __iter__(self) -> Iterator[Event]:
        while True:
            ready = dict(self._poller.poll(self._poll_ms))
            if not ready:
                yield Tick(self._clock())
                continue
            if self._monitor in ready:
                event = recv_monitor_message(self._monitor)
                kind: Literal["connected", "disconnected"] = (
                    "connected" if event["event"] == zmq.EVENT_CONNECTED else "disconnected"
                )
                yield Connection(kind, self._clock())
            if self._socket in ready:
                frames = self._socket.recv_multipart()
                yield Received(self._clock(), frames[0], tuple(frames[1:]))

    def close(self) -> None:
        self._socket.disable_monitor()
        self._monitor.close(linger=0)
        self._socket.close(linger=0)
        self._context.term()
