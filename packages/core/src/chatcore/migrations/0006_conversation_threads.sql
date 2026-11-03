CREATE TABLE conversations (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    thread_key text NOT NULL DEFAULT '',
    title text NOT NULL DEFAULT '',
    participants text[] NOT NULL DEFAULT '{}',
    reset_generation bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_active_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_scope_idx
    ON conversations (chat_key, thread_key, last_active_at DESC);

CREATE TABLE conversation_messages (
    chat_key text NOT NULL,
    message_id text NOT NULL,
    conversation_id bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    primary_message_id text NOT NULL,
    is_bot boolean NOT NULL,
    user_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, message_id)
);
CREATE INDEX conversation_messages_conv_idx ON conversation_messages (conversation_id);

CREATE TABLE conversation_summaries (
    conversation_id bigint PRIMARY KEY REFERENCES conversations (id) ON DELETE CASCADE,
    chat_key text NOT NULL,
    summary text NOT NULL,
    covers_until_turn_id bigint NOT NULL,
    reset_generation bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE conversation_turns
    ADD COLUMN conversation_id bigint REFERENCES conversations (id) ON DELETE CASCADE;
CREATE INDEX conversation_turns_conv_idx
    ON conversation_turns (conversation_id, id) WHERE conversation_id IS NOT NULL;
