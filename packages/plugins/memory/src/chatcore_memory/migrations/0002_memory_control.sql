CREATE TABLE memory_control (
    chat_key text PRIMARY KEY,
    epoch bigint NOT NULL DEFAULT 0,
    erasure_id text
);
