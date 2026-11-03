CREATE TABLE tutor_topics (
    chat_key text NOT NULL,
    thread_key text NOT NULL DEFAULT '',
    target text CHECK (char_length(target) <= 40),
    level text CHECK (level IN ('A1','A2','B1','B2','C1','C2')),
    explain text CHECK (char_length(explain) <= 40),
    auto_reply boolean NOT NULL DEFAULT false,
    set_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, thread_key),
    CHECK ((target IS NULL) = (level IS NULL)),
    CHECK (explain IS NULL OR target IS NOT NULL),
    CHECK (target IS NULL OR explain IS NULL OR target <> explain)
);
