CREATE TABLE administration_moderators (
    user_key text NOT NULL,
    chat_key text NOT NULL,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_key, chat_key)
);

CREATE TABLE administration_restrictions (
    user_key text NOT NULL,
    scope_key text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('block', 'pause')),
    until timestamptz,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_key, scope_key),
    CHECK ((kind = 'block') = (until IS NULL))
);

CREATE TABLE administration_user_limits (
    user_key text PRIMARY KEY,
    per_hour int NOT NULL CHECK (per_hour BETWEEN 1 AND 10000),
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE administration_audit ADD COLUMN detail text;
