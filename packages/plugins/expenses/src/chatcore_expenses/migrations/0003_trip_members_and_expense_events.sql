CREATE TABLE trip_members (
    trip_id uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    user_key text NOT NULL,
    added_by text NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trip_id, user_key)
);

CREATE TABLE expense_events (
    id bigserial PRIMARY KEY,
    chat_key text NOT NULL,
    expense_id uuid NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    actor_key text NOT NULL,
    action text NOT NULL CHECK (action IN ('add', 'update', 'delete', 'restore')),
    before jsonb,
    after jsonb,
    undo_of bigint REFERENCES expense_events(id),
    undone_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX expense_events_actor ON expense_events(chat_key, actor_key, id DESC);
CREATE INDEX expense_events_expense ON expense_events(expense_id, id DESC);
