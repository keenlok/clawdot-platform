CREATE TABLE display_time_preferences (
    chat_key    text        NOT NULL,
    subject_key text        NOT NULL DEFAULT '',
    zone        text        NOT NULL,
    updated_by  text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, subject_key)
);
