CREATE TABLE administration_budget (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    daily_tokens bigint CHECK (daily_tokens BETWEEN 100000 AND 100000000),
    extra_tokens bigint CHECK (extra_tokens BETWEEN 10000 AND 100000000),
    extra_day date,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((extra_tokens IS NULL) = (extra_day IS NULL))
);

CREATE TABLE budget_usage (
    day date PRIMARY KEY,
    tokens bigint NOT NULL CHECK (tokens >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
