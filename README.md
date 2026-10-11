# ov-reliability

How reliable is a Dutch tram, bus or metro line right now? This project records the real-time
BISON stream that Dutch operators publish through [NDOV Loket](https://govi.nu): KV6 (where each
vehicle is and how late), KV17 (cancelled and changed trips) and KV15 (messages to travellers,
such as a closed stop and why). It will turn them into punctuality figures served by an API.

The first part is the **recorder**. Punctuality history can't be fetched afterwards: it only
exists from the moment someone starts recording it. The recorder runs around the clock and
stores every message for GVB, HTM, RET and U-OV.

GVB publishes its metro (lines 50-54) only in SIRI, a second stream on NDOV Loket, so a second
recorder, `collector-siri`, runs the same code against that stream.

Next to it, the **timetable archive** keeps every published timetable. The timetable is what
tells a trip that never ran apart from one that was never planned, and the source only offers
the current version.

The **loader** reads the archive into PostgreSQL: the stop events and cancellations of the lines
the API will serve. It follows the file being written, so the database is about ten seconds
behind the stream.

## Running it

Requires Docker with the Compose plugin.

    git clone https://github.com/Deanddj/ov-reliability.git
    cd ov-reliability
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" > .env
    docker compose up -d --build

`.env` holds the database password and is not committed.

Updating:

    git pull
    docker compose up -d --build

Settings live in `config.toml`: topics, archive retention, service hours, silence limits, the
topics that should never go quiet and the timetable operators and check interval. The metro
recorder has its own `config.siri.toml` with the same sections, except `[timetables]` and
`[loader]`.

## Checking that it works

    docker compose logs --since 2h collector
    docker compose logs --since 2h collector-siri

Every hour the recorder logs one line about the hour that just ended, such as

    2026-10-07 15:00:02,114 INFO ov_reliability.collector.recorder 2026-10-07T14Z: 51249 records (/GVB/KV17cvlinfo=12, /GVB/KV6posinfo=20311, /IVU/KV6posinfo=1020, /RIG/KV17cvlinfo=35, /RIG/KV6posinfo=29870, _recorder=1), 0 bad payloads, 210.4 MB written, 61.2 GB free

The count includes the markers described below, listed under `_recorder`. The size is the
uncompressed size of the records.

A WARNING means something needs attention: a watched topic that has gone quiet during service
hours, a subscribed topic that sent nothing in the first hour, a payload that couldn't be
decoded, or low disk space. In the last case the hourly line itself is logged as a WARNING
and ends with `; low free disk, warning below 10 GB`.

## Timetables

The `timetables` service checks the NeTEx directories on
[data.ndovloket.nl](https://data.ndovloket.nl/netex/) every hour, for the operators listed in
`config.toml`, and downloads every file it hasn't stored yet. A replaced timetable disappears
from the source, and a new one only covers the days from its publication on, so the timetable
for a past day can only be found here.

Files are kept unchanged in the `timetables` volume as `<operator>/<original name>`. A file is
only stored after it has been decompressed in full and starts as XML. Nothing is overwritten:
if a file is republished under the same name with different content, the new version is
stored beside it as `<name>.<first 8 characters of its sha256>.xml.gz`.

`index.jsonl` has one line per stored file:

    {"stored_at":"2026-10-08T12:12:20.512Z","operator":"ret","name":"NeTEx_RET_ALL_20261005_20261005_version415_v930.xml.gz","file":"ret/NeTEx_RET_ALL_20261005_20261005_version415_v930.xml.gz","bytes":3191007,"sha256":"...","listed_modified":"05-Oct-2026 14:11"}

`listed_modified` is the modification time as the listing shows it (local time, no zone).

After each check the service logs one line:

    docker compose logs --since 2h timetables

    2026-10-08 12:12:35,790 INFO ov_reliability.timetables.archiver check done: 8 operators, 38 listed, 38 new (70.9 MB), 0 republished, 0 errors, 794.9 GB free

A listing that can't be fetched or a download that fails is a WARNING and is tried again at
the next check. A listing page that no longer looks like the one this parser knows is an ERROR,
and so is a check in which every operator failed.

At startup the service compares the index with the files. A file that was stored but never
indexed (a crash between the two steps) is added to the index with `listed_modified` set to
`null`, and a cut-off last index line is removed; both are logged as a WARNING. An index line
whose file is missing or has a different size stops the service, because something outside it
changed the archive.

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

    docker compose exec -T collector sh -c 'zcat /data/archive/$(ls -t /data/archive | head -1)' | head

The file being written is cut off at the last flush, so zcat may report an unexpected end of
file; the lines before it are fine.

The metro recorder writes the same files and markers to its own `archive-siri` volume.

## Database

The `loader` service reads the archive files in order and writes to the `postgres` service. It
remembers how far it got (file and record) in the same transaction as the rows, so a restart
never loads a message twice or skips one. On its first start it begins with the oldest file in
the archive.

What it stores, for the data owners in `[loader] data_owners`:

- `stop_events`: KV6 `ARRIVAL` and `DEPARTURE`, with the delay exactly as the operator reported
  it. These are what a departure's delay is read from.
- `trips_seen`: one row per trip that sent an `ARRIVAL`, `ONSTOP` or `DEPARTURE`. `ONSTOP` repeats
  every minute while a vehicle stands still, so it only counts here, not as rows of its own.
- `journey_mutations`: KV17 `CANCEL`, `RECOVER`, `NOTMONITORED` and `SHORTEN` (a skipped stop).
- `recorder_markers`, `load_progress` and `bad_messages`.

Position updates between stops (`ONROUTE`) and KV15 messages stay in the raw archive only.

The day tables have one partition per operating day. Days older than `window_days` (90) are
dropped once an hour.

A message that can't be read (broken XML, a missing field, a time that can't be right) is
stored in `bad_messages` with the reason, and loading goes on; the hourly line is then a
WARNING. A damaged archive file that is already complete stops the loader with an ERROR.

Every hour the loader logs one line per archive:

    docker compose logs --since 2h loader

    2026-10-09 09:00:06,012 INFO ov_reliability.loader Last hour from collector: 32765 records, 78492 stop events, 533 mutations, 0 bad messages; data until 2026-10-09T08:59:59.950000+00:00 (6 s behind)

### Replay

When the parser changes, the database is rebuilt from the raw archive:

    docker compose stop loader
    docker compose run --rm loader /app/.venv/bin/python -m ov_reliability.loader replay
    docker compose start loader

Replay empties the days from the day after the oldest archive file on, and the loader then
reads every file again. Older days stay as they are, because their raw files are gone.

## Development

Requires [uv](https://docs.astral.sh/uv/).

    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .
    uv run mypy

The database tests need a PostgreSQL server and are skipped without one:

    docker run --rm -d -p 127.0.0.1:5433:5432 -e POSTGRES_PASSWORD=test postgres:18
    OV_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres uv run pytest

Each test works in a schema of its own, which is dropped afterwards. CI always runs them.

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
- **Reversibility.** Raw messages and timetables are stored before anything reads them, and
  stored timetables are never overwritten, so a better parser can always be run over old data.

## Data

Real-time data and timetables from NDOV Loket, published under CC0.

## License

MIT
