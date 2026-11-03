CREATE TABLE poll_settings (
    chat_key text NOT NULL,
    is_dm boolean NOT NULL,
    polls_enabled boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, is_dm)
);

CREATE TABLE polls (
    id uuid PRIMARY KEY,
    chat_key text NOT NULL,
    is_dm boolean NOT NULL,
    thread_id text,
    number integer NOT NULL,
    creator_key text NOT NULL,
    creator_name text NOT NULL,
    question text NOT NULL,
    options jsonb NOT NULL CHECK (jsonb_typeof(options) = 'array'),
    multiple boolean NOT NULL,
    anonymous boolean NOT NULL,
    status text NOT NULL CHECK (status IN ('sending', 'open', 'closed', 'failed')),
    close_at timestamptz,
    platform_message_id text,
    platform_poll_id text,
    final_counts jsonb,
    total_voters integer,
    close_failures integer NOT NULL DEFAULT 0,
    lease_token uuid,
    lease_until timestamptz,
    source_action_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    UNIQUE (chat_key, number),
    UNIQUE (chat_key, source_action_id)
);
CREATE UNIQUE INDEX polls_platform_id ON polls
    (split_part(chat_key, ':', 1), platform_poll_id) WHERE platform_poll_id IS NOT NULL;
CREATE INDEX polls_due ON polls (status, close_at);
CREATE INDEX polls_scope ON polls (chat_key, is_dm, thread_id, status);

CREATE TABLE poll_votes (
    poll_id uuid NOT NULL REFERENCES polls ON DELETE CASCADE,
    chat_key text NOT NULL,
    voter_key text NOT NULL,
    voter_name text NOT NULL,
    option_index integer NOT NULL CHECK (option_index >= 0),
    voted_at timestamptz NOT NULL,
    PRIMARY KEY (poll_id, voter_key, option_index)
);
CREATE INDEX poll_votes_user ON poll_votes (chat_key, voter_key);
