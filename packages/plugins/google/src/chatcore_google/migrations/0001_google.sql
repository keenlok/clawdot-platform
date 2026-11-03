CREATE TABLE google_settings (
    chat_key TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    revision BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE google_actions (
    chat_key TEXT NOT NULL,
    action_id TEXT NOT NULL,
    requester_key TEXT NOT NULL,
    suggestion_rows INTEGER NOT NULL DEFAULT 0 CHECK (suggestion_rows >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, action_id)
);
CREATE INDEX google_actions_daily ON google_actions (chat_key, created_at);
CREATE TABLE google_itinerary_links (
    chat_key TEXT NOT NULL,
    file_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    requester_key TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, file_id)
);
