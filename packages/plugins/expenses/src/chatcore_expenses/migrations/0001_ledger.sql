CREATE TABLE trips (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_key text NOT NULL,
    name text NOT NULL,
    countries text[] NOT NULL,
    currencies text[] NOT NULL,
    settle_mode text NOT NULL CHECK (settle_mode IN ('convert', 'per_currency')),
    settle_currency text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'closed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);
CREATE UNIQUE INDEX trips_one_active_per_chat ON trips(chat_key) WHERE status = 'active';
CREATE INDEX trips_chat_started ON trips(chat_key, started_at DESC);
CREATE TABLE expenses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), chat_key text NOT NULL,
    trip_id uuid REFERENCES trips(id) ON DELETE CASCADE, description text NOT NULL,
    amount bigint NOT NULL CHECK (amount > 0), currency char(3) NOT NULL,
    paid_by text NOT NULL, created_by text NOT NULL, spent_on date NOT NULL,
    fx_rate_to_settle numeric, created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
    CHECK (fx_rate_to_settle IS NULL OR fx_rate_to_settle > 0)
);
CREATE INDEX expenses_chat_created ON expenses(chat_key, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX expenses_chat_trip ON expenses(chat_key, trip_id) WHERE deleted_at IS NULL;
CREATE TABLE expense_shares (
    expense_id uuid NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    user_key text NOT NULL, amount bigint NOT NULL CHECK (amount >= 0), PRIMARY KEY (expense_id, user_key)
);
CREATE TABLE settlements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), chat_key text NOT NULL,
    trip_id uuid REFERENCES trips(id) ON DELETE CASCADE, from_user text NOT NULL,
    to_user text NOT NULL, amount bigint NOT NULL CHECK (amount > 0), currency char(3) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), CHECK (from_user <> to_user)
);
CREATE INDEX settlements_chat_trip ON settlements(chat_key, trip_id);
CREATE TABLE fx_overrides (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_key text NOT NULL, trip_id uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    currency char(3) NOT NULL, settle_currency char(3) NOT NULL, rate numeric NOT NULL CHECK (rate > 0),
    set_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fx_overrides_lookup ON fx_overrides(
    chat_key, trip_id, currency, settle_currency, created_at DESC
);

CREATE FUNCTION enforce_expense_share_total() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_expense uuid;
    expense_total bigint;
    share_total bigint;
BEGIN
    IF TG_TABLE_NAME = 'expenses' THEN
        target_expense := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target_expense := OLD.expense_id;
    ELSE
        target_expense := NEW.expense_id;
    END IF;
    SELECT amount INTO expense_total FROM expenses WHERE id = target_expense;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT COALESCE(sum(amount), 0) INTO share_total
      FROM expense_shares WHERE expense_id = target_expense;
    IF expense_total <> share_total THEN
        RAISE EXCEPTION 'expense shares must sum to expense amount'
          USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER expense_share_total_on_expense
    AFTER INSERT OR UPDATE ON expenses DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_expense_share_total();
CREATE CONSTRAINT TRIGGER expense_share_total_on_share
    AFTER INSERT OR UPDATE OR DELETE ON expense_shares DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_expense_share_total();
