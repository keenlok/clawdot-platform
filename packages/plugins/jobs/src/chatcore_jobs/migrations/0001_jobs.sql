CREATE TABLE job_settings (
    chat_key text NOT NULL,
    is_dm boolean NOT NULL,
    reminders_enabled boolean NOT NULL DEFAULT false,
    watches_enabled boolean NOT NULL DEFAULT false,
    max_jobs_per_user integer,
    max_jobs_per_chat integer,
    minimum_interval_seconds integer,
    revision bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, is_dm),
    CHECK (max_jobs_per_user IS NULL OR max_jobs_per_user > 0),
    CHECK (max_jobs_per_chat IS NULL OR max_jobs_per_chat > 0),
    CHECK (minimum_interval_seconds IS NULL OR minimum_interval_seconds > 0)
);

CREATE TABLE scheduled_jobs (
    id uuid PRIMARY KEY,
    chat_key text NOT NULL,
    is_dm boolean NOT NULL,
    owner_key text NOT NULL,
    thread_id text,
    kind text NOT NULL CHECK (kind IN ('reminder', 'watch')),
    status text NOT NULL CHECK (status IN ('active', 'completed', 'cancelled', 'failed')),
    payload jsonb NOT NULL,
    source_action_id text NOT NULL,
    created_at timestamptz NOT NULL,
    terminal_at timestamptz,
    next_run_at timestamptz NOT NULL,
    occurrence integer NOT NULL DEFAULT 0 CHECK (occurrence >= 0),
    revision bigint NOT NULL DEFAULT 0,
    lease_token uuid,
    lease_until timestamptz,
    failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    error_episode boolean NOT NULL DEFAULT false,
    snapshot text,
    snapshot_hash text,
    last_delivery_state text,
    UNIQUE(chat_key, source_action_id)
);
CREATE INDEX scheduled_jobs_due ON scheduled_jobs(status, next_run_at);
CREATE INDEX scheduled_jobs_scope_owner ON scheduled_jobs(chat_key, is_dm, owner_key);
CREATE INDEX scheduled_jobs_scope_thread ON scheduled_jobs(chat_key, is_dm, thread_id, kind);

CREATE TABLE job_deliveries (
    chat_key text NOT NULL,
    job_id uuid NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
    occurrence integer NOT NULL,
    notification_kind text NOT NULL,
    status text NOT NULL CHECK (status IN ('reserved', 'sent', 'unknown', 'suppressed')),
    terminal_status text CHECK (terminal_status IS NULL OR terminal_status IN ('completed', 'failed')),
    content text NOT NULL CHECK (length(content) <= 1500),
    platform_message_id text,
    claim_token uuid NOT NULL,
    reserved_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY(chat_key, job_id, occurrence, notification_kind)
);
CREATE INDEX job_deliveries_retention ON job_deliveries(finished_at);
