CREATE TABLE privacy_erasures (
    operation_id text PRIMARY KEY,
    chat_key text NOT NULL,
    scope text NOT NULL CHECK (scope IN ('user', 'chat')),
    target_user_key text,
    status text NOT NULL CHECK (status IN ('active', 'queued')),
    completed_steps text[] NOT NULL DEFAULT '{}',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX privacy_erasures_active_chat_idx ON privacy_erasures (chat_key)
    WHERE status = 'active';
CREATE INDEX privacy_erasures_due_idx ON privacy_erasures (next_attempt_at, created_at);
