CREATE TABLE memories (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    subject_user_key text,
    text text NOT NULL CHECK (char_length(text) <= 500),
    source text NOT NULL CHECK (source IN ('explicit', 'auto')),
    pinned boolean NOT NULL DEFAULT false,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX memories_chat_id_idx ON memories (chat_key, id);
CREATE INDEX memories_chat_subject_idx ON memories (chat_key, subject_user_key);
