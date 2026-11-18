CREATE TABLE google_outputs (
    chat_key TEXT NOT NULL,
    file_id TEXT NOT NULL CHECK (char_length(file_id) BETWEEN 1 AND 200),
    mime TEXT NOT NULL,
    requester_key TEXT NOT NULL,
    thread_id TEXT,
    owner_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    trashed_at TIMESTAMPTZ,
    PRIMARY KEY (chat_key, file_id)
);
CREATE INDEX google_outputs_daily ON google_outputs (chat_key, requester_key, created_at);
ALTER TABLE google_settings ADD COLUMN confirm_writes BOOLEAN NOT NULL DEFAULT TRUE;
CREATE TABLE google_create_reservations (
    chat_key TEXT NOT NULL,
    action_id TEXT NOT NULL,
    requester_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, action_id)
);
CREATE INDEX google_create_reservations_daily
    ON google_create_reservations (chat_key, requester_key, created_at);
