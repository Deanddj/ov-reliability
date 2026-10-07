# ov-reliability

How reliable is a Dutch tram, bus or metro line right now? This project records the real-time
KV6/KV17 stream that Dutch operators publish through [NDOV Loket](https://govi.nu) and will turn
it into punctuality figures served by an API.

The first part is the **recorder**. Punctuality history can't be fetched afterwards: it only
exists from the moment someone starts recording it. The recorder runs around the clock and
stores every message for GVB, HTM, RET and U-OV.

## Running it

Requires Docker with the Compose plugin.

    git clone https://github.com/Deanddj/ov-reliability.git
    cd ov-reliability
    docker compose up -d --build

Updating:

    git pull
    docker compose up -d --build

Settings live in `config.toml`: topics, archive retention, service hours and silence limits.

## Checking that it works

    docker compose logs --since 2h collector

Every hour the recorder logs one line about the hour that just ended, such as

    2026-10-07 15:00:02,114 INFO ov_reliability.collector.recorder 2026-10-07T14Z: 51249 records (/GVB/KV17cvlinfo=12, /GVB/KV6posinfo=20311, /IVU/KV6posinfo=1020, /RIG/KV17cvlinfo=35, /RIG/KV6posinfo=29870, _recorder=1), 0 bad payloads, 210.4 MB written, 61.2 GB free

The count includes the markers described below, listed under `_recorder`. The size is the
uncompressed size of the records.

A WARNING means something needs attention: a topic that has gone quiet during service hours,
a subscribed topic that sent nothing in the first hour, a payload that couldn't be decoded, or
low disk space. In the last case the hourly line itself is logged as a WARNING and ends with
`; low free disk, warning below 10 GB`.

## The archive

Messages are stored as one gzipped JSON Lines file per hour in the `archive` volume, named by
UTC hour and a sequence number: `2026-10-07T14Z.0.jsonl.gz`. After a restart within the same
hour the recorder starts a new file with the next number. Each line is one record:

    {"received_at":"2026-10-07T14:03:12.481Z","topic":"/RIG/KV6posinfo","payload":"<?xml ..."}

A payload that can't be decoded is kept as `payload_b64` (the raw bytes, base64) with an
`error` field instead of `payload`.

Lines with `"topic":"_recorder"` are markers (`started`, `stopped`, `connected`,
`disconnected`, `silence_start`, `silence_end`). They make every gap explainable: later,
"no data" can be told apart from "no traffic".

To look at the latest file:

    docker compose exec -T collector sh -c 'zcat /data/archive/$(ls /data/archive | tail -1)' | head

The file being written is cut off at the last flush, so zcat may report an unexpected end of
file; the lines before it are fine.

## Development

Requires [uv](https://docs.astral.sh/uv/).

    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .
    uv run mypy

## Design

The design follows ideas from *The Pragmatic Programmer*. A few places where they made a
difference:

- **Tracer bullets.** The recorder runs on its own first. Parsing, storage and the API come
  later and will be filled by replaying the archive, so no recorded data waits on them.
- **Crash early.** When every topic stays silent during service hours, the recorder exits and
  Docker restarts it with a fresh connection, instead of waiting on a connection that has died
  quietly.
- **Configuration.** Topics, retention and service hours are settings, so covering another
  operator is an edit to `config.toml`, not to the code.

## Data

Real-time data from NDOV Loket, published under CC0.

## License

MIT
