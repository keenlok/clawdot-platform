-- Explicit maintenance install after base schema.sql and broker_schema.sql.
CREATE TABLE IF NOT EXISTS release_control.broker_dispatch_intent (
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    dispatch_fence bigint NOT NULL CHECK (dispatch_fence>0),
    dispatch_id uuid NOT NULL UNIQUE,
    request_digest bytea NOT NULL CHECK (octet_length(request_digest)=32),
    state text NOT NULL CHECK (state IN ('sending','accepted','uncertain','retired')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (approval_id,dispatch_fence)
);
CREATE TABLE IF NOT EXISTS release_control.broker_run_observation (
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    dispatch_fence bigint NOT NULL,
    run_id bigint NOT NULL,
    run_attempt integer NOT NULL,
    status text NOT NULL CHECK (status IN ('queued','in_progress','completed','waiting','pending','requested')),
    conclusion text,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (approval_id,dispatch_fence,observed_at)
);
CREATE TABLE IF NOT EXISTS release_control.broker_report_delivery (
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    release_status text NOT NULL,
    request_id uuid NOT NULL UNIQUE,
    delivered boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (approval_id,release_status)
);
CREATE OR REPLACE FUNCTION release_control.guard_worker_record()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR TG_TABLE_NAME='broker_run_observation'
        OR OLD.state='retired'
        OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
        OR NEW.dispatch_fence IS DISTINCT FROM OLD.dispatch_fence
        OR NEW.dispatch_id IS DISTINCT FROM OLD.dispatch_id
        OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
        OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'immutable broker worker record';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS broker_dispatch_intent_guard ON release_control.broker_dispatch_intent;
CREATE TRIGGER broker_dispatch_intent_guard BEFORE UPDATE OR DELETE ON release_control.broker_dispatch_intent
FOR EACH ROW EXECUTE FUNCTION release_control.guard_worker_record();
DROP TRIGGER IF EXISTS broker_run_observation_guard ON release_control.broker_run_observation;
CREATE TRIGGER broker_run_observation_guard BEFORE UPDATE OR DELETE ON release_control.broker_run_observation
FOR EACH ROW EXECUTE FUNCTION release_control.guard_worker_record();
REVOKE ALL ON release_control.broker_dispatch_intent,release_control.broker_run_observation,
release_control.broker_report_delivery FROM PUBLIC;
REVOKE ALL ON FUNCTION release_control.guard_worker_record() FROM PUBLIC;
