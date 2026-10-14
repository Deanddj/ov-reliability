# ov-reliability

Can I trust this tram right now? An API that compares the timetable of Dutch trams, buses and
metros with what actually happened, trip by trip, from the real-time data operators publish
through [NDOV Loket](https://ndovloket.nl).

Live at **https://ov.deanj.dev** for all HTM trams and buses in The Hague. More operators follow.

## Example

    curl 'https://ov.deanj.dev/v1/lines/htm/tram/3/summary?from=2026-10-08T19:00:00Z&to=2026-10-08T20:00:00Z'

```json
{
  "direction": "outbound",
  "trips": {"total": 8, "ran": 8, "cancelled": 0, "missing": 0, "unknown": 0, "not_yet_due": 0, "unplanned": 0},
  "departures": {"total": 164, "with_delay": 76, "without_delay": 88, "cancelled": 45},
  "delay_seconds": {"p10": 130, "p50": 882, "p90": 4481}
}
```

That was a bad evening: every tram came, but 45 stops were skipped and the slowest tenth of
departures left more than 74 minutes late.

`/trips` gives the same hour trip by trip, every stop with its planned and actual time:

```json
{
  "journey_number": 30199,
  "direction": "inbound",
  "planned_start": "2026-10-08T20:42:26+02:00",
  "status": "ran",
  "departures": [
    {"stop_name": "Van Speijkstraat", "planned": "2026-10-08T21:30:29+02:00",
     "actual": "2026-10-08T21:34:19+02:00", "delay_seconds": 228, "cancelled": false}
  ]
}
```

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /v1/lines` | The lines served |
| `GET /v1/lines/{operator}/{mode}/{code}/trips` | Every trip in a time range, with its status and departures |
| `GET /v1/lines/{operator}/{mode}/{code}/summary` | Counts and delay percentiles per direction |

`from` and `to` take ISO 8601 times with an offset (`Z`, or `+02:00` sent as `%2B02:00`); the
default is the last two hours, the maximum seven days. `direction` is `inbound` or `outbound`.
The full schema is at [/docs](https://ov.deanj.dev/docs).

| Status | Meaning |
|---|---|
| `ran` | The tram reported at a stop |
| `cancelled` | The operator cancelled it and it never showed up |
| `missing` | It should have started 15 minutes ago and nothing was heard |
| `unknown` | As missing, but the recording had a gap, so it can't be told |
| `not_yet_due` | Not 15 minutes past its planned start yet |

Delays are the ones the operator reported, in seconds. Percentiles use the nearest rank, so each
one is a delay that actually happened.

## How it works

Two recorders store every real-time message (KV6, KV17, SIRI) in hourly files, an archiver keeps
every published NeTEx timetable, a loader puts stop events and cancellations into PostgreSQL, and
the API joins them with the timetable on each request. The raw data is kept, so a better parser
can always be run over the past. More in [docs/operations.md](docs/operations.md).

## Running it

    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" > .env
    docker compose up -d --build

The API listens on `127.0.0.1:8470` (`API_PORT` in `.env` changes it). Lines, operators and
thresholds are set in `config.toml`.

## Development

    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .
    uv run mypy

Database tests need PostgreSQL and are skipped without it:

    docker run --rm -d -p 127.0.0.1:5433:5432 -e POSTGRES_PASSWORD=test postgres:18
    OV_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres uv run pytest

## Data

Real-time data and timetables from NDOV Loket, CC0. Code under the MIT license.
