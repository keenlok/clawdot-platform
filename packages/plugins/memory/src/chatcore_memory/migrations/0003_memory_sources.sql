ALTER TABLE memories ADD CONSTRAINT memories_chat_id_unique UNIQUE (chat_key, id);
ALTER TABLE memories ADD COLUMN provenance text NOT NULL DEFAULT 'legacy_unknown'
    CHECK (provenance IN ('known', 'request_only', 'none', 'legacy_unknown'));

CREATE TABLE memory_sources (
    chat_key text NOT NULL,
    memory_id bigint NOT NULL,
    source_message_id text NOT NULL,
    source_author_key text NOT NULL,
    source_role text NOT NULL CHECK (source_role IN ('statement', 'context', 'request')),
    policy_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_key, memory_id, source_message_id),
    FOREIGN KEY (chat_key, memory_id) REFERENCES memories (chat_key, id) ON DELETE CASCADE
);
CREATE INDEX memory_sources_author_idx ON memory_sources (chat_key, source_author_key, memory_id);

CREATE TABLE memory_index_work (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    memory_id bigint,
    op text NOT NULL CHECK (op IN ('verify_index', 'delete_doc', 'delete_chat')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_index_work_due_idx ON memory_index_work (next_attempt_at, id);
