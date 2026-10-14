-- The API reads a line's stop events of a few days. Without these columns in the index each row
-- costs a heap page, because a line's rows are spread over the whole day partition.

ALTER TABLE stop_events
    DROP CONSTRAINT stop_events_operating_day_data_owner_planning_number_journe_key;

ALTER TABLE stop_events
    ADD UNIQUE (operating_day, data_owner, planning_number, journey_number, reinforcement,
                stop_code, passage, event, event_at)
    INCLUDE (punctuality, received_at);
