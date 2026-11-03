-- Maintenance-only source schema. The running service must never apply migrations.
CREATE SCHEMA IF NOT EXISTS release_control;
REVOKE ALL ON SCHEMA release_control FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON FUNCTIONS FROM PUBLIC;

CREATE TABLE release_control.telegram_relay_event (
    relay_id uuid PRIMARY KEY,
    principal text NOT NULL CHECK (length(principal) BETWEEN 1 AND 128),
    update_id bigint NOT NULL CHECK (update_id > 0),
    payload jsonb NOT NULL,
    payload_sha256 bytea NOT NULL CHECK (octet_length(payload_sha256) = 32),
    created_at timestamptz NOT NULL,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    acknowledged_at timestamptz,
    UNIQUE (principal, update_id)
);

CREATE INDEX telegram_relay_pending_idx
ON release_control.telegram_relay_event (principal, created_at)
WHERE acknowledged_at IS NULL;

REVOKE ALL ON release_control.telegram_relay_event FROM PUBLIC;
