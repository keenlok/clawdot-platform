CREATE TABLE context_resets (
    chat_key text NOT NULL,
    thread_key text NOT NULL DEFAULT '',
    reset_at timestamptz NOT NULL,
    after_turn_id bigint NOT NULL DEFAULT 0,
    reset_by text NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_key, thread_key)
);

ALTER TABLE chat_summaries
    ADD COLUMN reset_generation bigint NOT NULL DEFAULT 0;
