-- Maintenance-only; the running bridge never creates tables or grants privileges.
CREATE TABLE IF NOT EXISTS release_control.bridge_broker_send (
    request_id uuid PRIMARY KEY,
    request_digest bytea NOT NULL CHECK (octet_length(request_digest)=32),
    state text NOT NULL CHECK (state IN ('sending','complete','uncertain')),
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK ((state='complete')=(response IS NOT NULL)),
    CHECK ((state='complete')=(completed_at IS NOT NULL))
);
CREATE OR REPLACE FUNCTION release_control.guard_bridge_send()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.state <> 'sending'
        OR NEW.request_id IS DISTINCT FROM OLD.request_id
        OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
        OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'immutable bridge send';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS bridge_broker_send_guard ON release_control.bridge_broker_send;
CREATE TRIGGER bridge_broker_send_guard BEFORE UPDATE OR DELETE ON release_control.bridge_broker_send
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_bridge_send();
REVOKE ALL ON release_control.bridge_broker_send FROM PUBLIC;
REVOKE ALL ON FUNCTION release_control.guard_bridge_send() FROM PUBLIC;

CREATE TABLE IF NOT EXISTS release_control.release_card_view (
    approval_id uuid PRIMARY KEY REFERENCES release_control.release_card(id),
    presentation jsonb NOT NULL,
    patch_state text NOT NULL CHECK (patch_state IN ('included','too_large','unavailable')),
    patch bytea CHECK (patch IS NULL OR octet_length(patch) <= 393216),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((patch_state='included')=(patch IS NOT NULL))
);
CREATE OR REPLACE FUNCTION release_control.guard_release_card_view()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable release card view';
END;
$$;
DROP TRIGGER IF EXISTS release_card_view_guard ON release_control.release_card_view;
CREATE TRIGGER release_card_view_guard BEFORE UPDATE OR DELETE ON release_control.release_card_view
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_release_card_view();
REVOKE ALL ON release_control.release_card_view FROM PUBLIC;
REVOKE ALL ON FUNCTION release_control.guard_release_card_view() FROM PUBLIC;
