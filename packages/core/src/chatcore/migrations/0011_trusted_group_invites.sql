-- All changes are serialized by administration_revision, including join admission.
CREATE TABLE administration_trusted_inviters (
    user_key text PRIMARY KEY,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE administration_group_blocks (
    chat_key text PRIMARY KEY,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE administration_group_admissions (
    chat_key text PRIMARY KEY,
    inviter_key text NOT NULL,
    admitted_at timestamptz NOT NULL DEFAULT now()
);

-- Distinguish redelivery from a genuine rejoin, including across process restarts.
CREATE TABLE administration_join_events (
    chat_key text NOT NULL,
    event_key text NOT NULL,
    PRIMARY KEY (chat_key, event_key)
);
