CREATE TABLE extension_requests (
    chat_key text NOT NULL,
    id text NOT NULL,
    author_key text NOT NULL,
    description text NOT NULL,
    examples jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('requested', 'building', 'built', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, id)
);
CREATE INDEX extension_requests_scope_state ON extension_requests (chat_key, state, created_at);

CREATE TABLE extension_revisions (
    chat_key text NOT NULL,
    id text NOT NULL,
    request_id text NOT NULL,
    author_key text NOT NULL,
    artifact jsonb NOT NULL,
    digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, id),
    FOREIGN KEY (chat_key, request_id) REFERENCES extension_requests (chat_key, id) ON DELETE CASCADE,
    UNIQUE (chat_key, request_id)
);

CREATE TABLE extension_evidence (
    chat_key text NOT NULL,
    revision_id text NOT NULL,
    digest text NOT NULL,
    passed boolean NOT NULL,
    image_id text NOT NULL,
    policy_digest text NOT NULL,
    harness_digest text NOT NULL,
    case_count integer NOT NULL CHECK (case_count > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, revision_id),
    FOREIGN KEY (chat_key, revision_id) REFERENCES extension_revisions (chat_key, id) ON DELETE CASCADE
);

CREATE TABLE extension_activations (
    chat_key text NOT NULL,
    revision_id text NOT NULL,
    digest text NOT NULL,
    reviewer_key text NOT NULL,
    epoch bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'suspended', 'revoked')),
    failures integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, revision_id),
    FOREIGN KEY (chat_key, revision_id) REFERENCES extension_revisions (chat_key, id) ON DELETE CASCADE
);

CREATE TABLE extension_runs (
    chat_key text NOT NULL,
    run_id text NOT NULL,
    revision_id text NOT NULL,
    digest text NOT NULL,
    epoch bigint NOT NULL,
    actor_key text NOT NULL,
    status text NOT NULL,
    duration_ms integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (chat_key, run_id),
    FOREIGN KEY (chat_key, revision_id) REFERENCES extension_revisions (chat_key, id) ON DELETE CASCADE
);

CREATE TABLE extension_quota (
    chat_key text NOT NULL,
    utc_day date NOT NULL,
    builds integer NOT NULL DEFAULT 0,
    runs integer NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_key, utc_day)
);
