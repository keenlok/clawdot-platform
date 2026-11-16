-- Changes are serialized by administration_revision.
CREATE TABLE administration_dm_grants (
    user_key text PRIMARY KEY,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);
