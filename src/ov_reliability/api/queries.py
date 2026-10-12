"""Reading what the loader stored: observations, recorder markers and how far loading got."""

from collections.abc import Collection
from datetime import date, datetime

import psycopg

from ov_reliability.collector.records import Marker
from ov_reliability.kv6.parse import Mutation, StopEvent, TripKey
from ov_reliability.stats.trips import Observations

_TRIP = "operating_day, data_owner, planning_number, journey_number, reinforcement"
_WHERE = "data_owner = %s AND planning_number = ANY(%s) AND operating_day = ANY(%s)"


class Reader:
    """Reads what the loader stored, over one connection."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def observations(
        self, owner: str, planning_numbers: Collection[str], days: Collection[date]
    ) -> dict[TripKey, Observations]:
        params = (owner, list(planning_numbers), list(days))
        events: dict[TripKey, list[StopEvent]] = {}
        for row in self._conn.execute(
            f"SELECT {_TRIP}, stop_code, passage, event, event_at, punctuality, received_at"
            f" FROM stop_events WHERE {_WHERE}",
            params,
        ):
            key = TripKey(*row[:5])
            events.setdefault(key, []).append(StopEvent(key, *row[5:]))
        mutations: dict[TripKey, list[Mutation]] = {}
        for row in self._conn.execute(
            f"SELECT {_TRIP}, mutation, stop_code, passage, mutated_at, received_at"
            f" FROM journey_mutations WHERE {_WHERE}",
            params,
        ):
            key = TripKey(*row[:5])
            mutations.setdefault(key, []).append(Mutation(key, *row[5:]))
        seen: dict[TripKey, tuple[datetime, datetime]] = {
            TripKey(*row[:5]): (row[5], row[6])
            for row in self._conn.execute(
                f"SELECT {_TRIP}, first_seen_at, last_seen_at FROM trips_seen WHERE {_WHERE}",
                params,
            )
        }
        return {
            key: Observations(
                tuple(events.get(key, ())), tuple(mutations.get(key, ())), seen.get(key)
            )
            for key in events.keys() | mutations.keys() | seen.keys()
        }

    def markers(self, recorder: str, start: datetime, end: datetime) -> list[Marker]:
        """The markers up to `end`, from the last `started` at or before `start` on.

        Starting at that `started` gives the gap rules the state at `start`: whether the
        recorder ran, and which disconnects and silences were already open.
        """
        rows = self._conn.execute(
            """
            SELECT at, event, details FROM recorder_markers
            WHERE recorder = %(recorder)s AND at <= %(end)s AND at >= COALESCE(
                (SELECT max(at) FROM recorder_markers
                 WHERE recorder = %(recorder)s AND event = 'started' AND at <= %(start)s),
                '-infinity')
            ORDER BY at, event
            """,
            {"recorder": recorder, "start": start, "end": end},
        )
        return [Marker(at, event, details) for at, event, details in rows]

    def data_until(self) -> dict[str, datetime]:
        """Per recorder, the receive time of the last record the loader stored (decision 077)."""
        rows = self._conn.execute(
            "SELECT recorder, data_until FROM load_progress WHERE data_until IS NOT NULL"
        )
        return {recorder: until for recorder, until in rows}
