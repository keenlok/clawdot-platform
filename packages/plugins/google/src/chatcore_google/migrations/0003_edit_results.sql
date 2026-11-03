CREATE TABLE google_edit_results (
    chat_key TEXT NOT NULL,
    action_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('edit_doc', 'edit_sheet', 'browser_edit_doc')),
    phase TEXT NOT NULL CHECK (phase IN (
        'reserved', 'destination_created', 'shared', 'applying', 'verified',
        'conflict', 'partial', 'uncertain'
    )),
    source_id TEXT NOT NULL,
    destination_id TEXT,
    destination_creation_claimed BOOLEAN NOT NULL DEFAULT FALSE,
    failed_stage TEXT,
    error_category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, action_id),
    FOREIGN KEY (chat_key, action_id)
        REFERENCES google_actions(chat_key, action_id) ON DELETE CASCADE,
    CHECK (char_length(source_id) BETWEEN 1 AND 200),
    CHECK (destination_id IS NULL OR char_length(destination_id) BETWEEN 1 AND 200),
    CHECK (failed_stage IS NULL OR char_length(failed_stage) <= 64),
    CHECK (error_category IS NULL OR char_length(error_category) <= 64)
);
CREATE INDEX google_edit_results_retention_idx
    ON google_edit_results(phase, created_at);
