-- chatcore core tables. Chat and user IDs are namespaced keys: '<platform>:<id>' (§3d).

CREATE TABLE members (
    chat_key      text        NOT NULL,
    user_key      text        NOT NULL,
    username      text,
    display_name  text        NOT NULL,
    is_bot        boolean     NOT NULL DEFAULT false,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    left_at       timestamptz,
    PRIMARY KEY (chat_key, user_key)
);

CREATE TABLE messages (
    id              bigserial   PRIMARY KEY,
    chat_key        text        NOT NULL,
    message_id      text        NOT NULL,
    thread_id       text,
    user_key        text        NOT NULL,
    display_name    text        NOT NULL,
    text            text        NOT NULL,
    attachments     jsonb       NOT NULL DEFAULT '[]',
    reply_to_id     text,
    is_bot          boolean     NOT NULL DEFAULT false,
    sent_at         timestamptz NOT NULL,
    UNIQUE (chat_key, message_id)
);
CREATE INDEX messages_chat_time ON messages (chat_key, sent_at DESC);
CREATE INDEX messages_chat_user ON messages (chat_key, user_key);

CREATE TABLE chat_settings (
    chat_key    text        PRIMARY KEY,
    persona     text        CHECK (char_length(persona) <= 500),
    chatmode    boolean     NOT NULL DEFAULT false,
    updated_by  text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- What a Confirm button (or reply code) refers to. Single use, expires (§6a).
CREATE TABLE pending_actions (
    id              text        PRIMARY KEY,
    chat_key        text        NOT NULL,
    thread_id       text,
    requester_key   text        NOT NULL,
    plugin          text        NOT NULL,
    kind            text        NOT NULL,
    payload         jsonb       NOT NULL,
    summary         text        NOT NULL,
    reply_code      text        NOT NULL,
    created_at      timestamptz NOT NULL,
    expires_at      timestamptz NOT NULL,
    used_at         timestamptz
);
CREATE INDEX pending_actions_code ON pending_actions (chat_key, requester_key, reply_code)
    WHERE used_at IS NULL;
