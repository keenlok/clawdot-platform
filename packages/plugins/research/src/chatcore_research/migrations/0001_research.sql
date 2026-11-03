CREATE TABLE research_jobs (
    id uuid PRIMARY KEY,
    chat_key text NOT NULL,
    is_dm boolean NOT NULL,
    thread_id text,
    owner_key text NOT NULL,
    topic text NOT NULL CHECK (length(topic) <= 2000),
    seed_urls jsonb NOT NULL DEFAULT '[]',
    status text NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    tokens_used integer NOT NULL DEFAULT 0,
    lease_token uuid,
    lease_until timestamptz,
    delivery_state text CHECK (delivery_state IS NULL OR delivery_state IN ('reserved','sent','unknown','suppressed')),
    report_message_id text,
    error_class text,
    created_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz
);
CREATE UNIQUE INDEX research_jobs_one_active_per_owner
    ON research_jobs(owner_key) WHERE status IN ('queued','running');
CREATE INDEX research_jobs_due ON research_jobs(status, lease_until, created_at);
CREATE INDEX research_jobs_scope_owner ON research_jobs(chat_key, owner_key, created_at DESC);
CREATE INDEX research_jobs_retention ON research_jobs(finished_at);
