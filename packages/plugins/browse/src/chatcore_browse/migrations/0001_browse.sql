CREATE TABLE browse_settings (
    chat_key text NOT NULL,
    is_dm boolean NOT NULL,
    rendered_enabled boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 0,
    updated_by text,
    updated_at timestamptz,
    PRIMARY KEY (chat_key, is_dm)
);
