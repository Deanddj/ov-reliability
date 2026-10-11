-- Day tables are partitioned by operating day; the loader creates and drops the partitions.

CREATE TABLE stop_events (
    operating_day date NOT NULL,
    data_owner text NOT NULL,
    planning_number text NOT NULL,
    journey_number integer NOT NULL,
    reinforcement smallint NOT NULL,
    stop_code text NOT NULL,
    passage smallint NOT NULL,
    event text NOT NULL CHECK (event IN ('ARRIVAL', 'DEPARTURE')),
    event_at timestamptz NOT NULL,
    punctuality integer NOT NULL,
    received_at timestamptz NOT NULL,
    source text NOT NULL CHECK (source IN ('KV6', 'SIRI')),
    UNIQUE (operating_day, data_owner, planning_number, journey_number, reinforcement,
            stop_code, passage, event, event_at)
) PARTITION BY RANGE (operating_day);

CREATE TABLE trips_seen (
    operating_day date NOT NULL,
    data_owner text NOT NULL,
    planning_number text NOT NULL,
    journey_number integer NOT NULL,
    reinforcement smallint NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL CHECK (last_seen_at >= first_seen_at),
    PRIMARY KEY (operating_day, data_owner, planning_number, journey_number, reinforcement)
) PARTITION BY RANGE (operating_day);

CREATE TABLE journey_mutations (
    operating_day date NOT NULL,
    data_owner text NOT NULL,
    planning_number text NOT NULL,
    journey_number integer NOT NULL,
    reinforcement smallint NOT NULL,
    mutation text NOT NULL CHECK (mutation IN ('CANCEL', 'RECOVER', 'NOTMONITORED', 'SHORTEN')),
    stop_code text,
    passage smallint,
    mutated_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    source text NOT NULL CHECK (source IN ('KV17', 'SIRI')),
    CHECK ((mutation = 'SHORTEN') = (stop_code IS NOT NULL AND passage IS NOT NULL)),
    UNIQUE NULLS NOT DISTINCT (operating_day, data_owner, planning_number, journey_number,
                               reinforcement, mutation, stop_code, passage, mutated_at)
) PARTITION BY RANGE (operating_day);

CREATE TABLE recorder_markers (
    recorder text NOT NULL,
    event text NOT NULL,
    at timestamptz NOT NULL,
    details jsonb NOT NULL,
    -- Silence markers for several topics can share a moment, so the details are part of the key.
    PRIMARY KEY (recorder, at, event, details)
);

CREATE TABLE load_progress (
    recorder text PRIMARY KEY,
    file_name text,
    records integer NOT NULL CHECK (records >= 0),
    data_until timestamptz,
    floor date
);

CREATE TABLE bad_messages (
    recorder text NOT NULL,
    received_at timestamptz NOT NULL,
    topic text NOT NULL,
    reason text NOT NULL
);

CREATE INDEX bad_messages_received_at ON bad_messages (received_at);
