CREATE TABLE administration_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL DEFAULT 0
);
INSERT INTO administration_revision(singleton, revision) VALUES (true, 0);

CREATE TABLE administration_groups (
    chat_key text PRIMARY KEY,
    enabled boolean NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE administration_assignments (
    user_key text NOT NULL,
    chat_key text NOT NULL,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_key, chat_key)
);

CREATE TABLE administration_audit (
    id bigserial PRIMARY KEY,
    revision bigint NOT NULL,
    actor_key text NOT NULL,
    operation text NOT NULL,
    chat_key text NOT NULL,
    subject_key text,
    created_at timestamptz NOT NULL DEFAULT now()
);
