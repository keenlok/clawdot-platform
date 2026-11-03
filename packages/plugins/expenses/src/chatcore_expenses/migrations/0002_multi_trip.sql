ALTER TABLE trips DROP CONSTRAINT trips_status_check;
ALTER TABLE trips ADD CONSTRAINT trips_status_check
    CHECK (status IN ('planned', 'active', 'closed'));
ALTER TABLE trips ADD COLUMN start_date date, ADD COLUMN end_date date;
ALTER TABLE trips ADD CONSTRAINT trips_dates_order
    CHECK (start_date IS NULL OR end_date IS NULL OR end_date >= start_date);
DROP INDEX trips_one_active_per_chat;
CREATE UNIQUE INDEX trips_open_name_per_chat ON trips(chat_key, lower(name))
    WHERE status <> 'closed';
CREATE TABLE trip_defaults (
    chat_key text PRIMARY KEY,
    trip_id uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    set_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO trip_defaults(chat_key, trip_id)
    SELECT chat_key, id FROM trips WHERE status = 'active';
