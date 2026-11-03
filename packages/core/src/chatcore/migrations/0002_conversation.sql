CREATE TABLE conversation_turns (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    thread_id text,
    role text NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content jsonb NOT NULL,
    message_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversation_turns_scope_idx
    ON conversation_turns (chat_key, coalesce(thread_id, ''), id DESC);

CREATE TABLE chat_summaries (
    chat_key text NOT NULL,
    thread_id text,
    summary text NOT NULL,
    covers_until_turn_id bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX chat_summaries_scope_idx
    ON chat_summaries (chat_key, coalesce(thread_id, ''));

CREATE TABLE token_usage (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    user_key text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX token_usage_day_idx ON token_usage (created_at, chat_key);
