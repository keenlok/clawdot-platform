-- Maintenance-only broker schema. Run after schema.sql as the protected installer.
-- The application validates readiness and never applies this file on startup.
CREATE SCHEMA IF NOT EXISTS release_control;
REVOKE ALL ON SCHEMA release_control FROM PUBLIC;

CREATE TABLE IF NOT EXISTS release_control.broker_schema_version (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL CHECK (version = 1),
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO release_control.broker_schema_version (singleton, version)
VALUES (true, 1) ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS release_control.broker_target_state (
    target text PRIMARY KEY REFERENCES release_control.target_queue(target),
    repository text NOT NULL CHECK (repository ~ '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$'),
    telegram_owner_user_id bigint NOT NULL CHECK (telegram_owner_user_id > 0),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    workflow_sha text NOT NULL CHECK (workflow_sha ~ '^[0-9a-f]{40}$'),
    policy_manifest jsonb NOT NULL CHECK (jsonb_typeof(policy_manifest) = 'object'),
    deployed_identity jsonb NOT NULL CHECK (jsonb_typeof(deployed_identity) = 'object'),
    generation bigint NOT NULL CHECK (generation > 0),
    reconciliation_required boolean NOT NULL DEFAULT false,
    pending_operation_id uuid,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (pending_operation_id IS NULL OR reconciliation_required)
);

CREATE TABLE IF NOT EXISTS release_control.broker_policy_snapshot (
    target text NOT NULL REFERENCES release_control.target_queue(target),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    repository text NOT NULL CHECK (repository ~ '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$'),
    workflow_sha text NOT NULL CHECK (workflow_sha ~ '^[0-9a-f]{40}$'),
    policy_manifest jsonb NOT NULL CHECK (jsonb_typeof(policy_manifest) = 'object'),
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (target, policy_digest)
);

CREATE OR REPLACE FUNCTION release_control.snapshot_broker_target_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO release_control.broker_policy_snapshot
        (target,policy_digest,repository,workflow_sha,policy_manifest)
    VALUES (NEW.target,NEW.policy_digest,NEW.repository,NEW.workflow_sha,NEW.policy_manifest)
    ON CONFLICT (target,policy_digest) DO NOTHING;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS broker_target_policy_snapshot ON release_control.broker_target_state;
CREATE TRIGGER broker_target_policy_snapshot
    AFTER INSERT OR UPDATE OF policy_digest,repository,workflow_sha,policy_manifest
    ON release_control.broker_target_state
    FOR EACH ROW EXECUTE FUNCTION release_control.snapshot_broker_target_policy();

CREATE TABLE IF NOT EXISTS release_control.broker_merge_lease (
    lease_id uuid PRIMARY KEY,
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    target text NOT NULL REFERENCES release_control.target_queue(target),
    dispatch_fence bigint NOT NULL CHECK (dispatch_fence > 0),
    repository_id bigint NOT NULL CHECK (repository_id > 0),
    run_id bigint NOT NULL CHECK (run_id > 0),
    run_attempt integer NOT NULL CHECK (run_attempt > 0),
    check_run_id bigint NOT NULL CHECK (check_run_id > 0),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    released_at timestamptz,
    CHECK (expires_at > created_at),
    UNIQUE (approval_id, dispatch_fence, lease_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS broker_one_live_merge_lease
    ON release_control.broker_merge_lease (approval_id)
    WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS release_control.broker_merge_admission (
    approval_id uuid PRIMARY KEY REFERENCES release_control.release_card(id),
    target text NOT NULL REFERENCES release_control.target_queue(target),
    dispatch_fence bigint NOT NULL CHECK (dispatch_fence > 0),
    lease_id uuid NOT NULL UNIQUE REFERENCES release_control.broker_merge_lease(lease_id),
    run_id bigint NOT NULL CHECK (run_id > 0),
    run_attempt integer NOT NULL CHECK (run_attempt > 0),
    check_run_id bigint NOT NULL CHECK (check_run_id > 0),
    expected_merge_tree text NOT NULL CHECK (expected_merge_tree ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
    evidence_digest text NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
    admitted_at timestamptz NOT NULL,
    start_deadline timestamptz NOT NULL,
    started_at timestamptz,
    CHECK (start_deadline > admitted_at)
);

CREATE TABLE IF NOT EXISTS release_control.broker_merge_observation (
    observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    target text NOT NULL REFERENCES release_control.target_queue(target),
    dispatch_fence bigint NOT NULL CHECK (dispatch_fence > 0),
    lease_id uuid NOT NULL REFERENCES release_control.broker_merge_lease(lease_id),
    merge_commit text NOT NULL CHECK (merge_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
    parents jsonb NOT NULL CHECK (jsonb_typeof(parents) = 'array'),
    merge_tree text NOT NULL CHECK (merge_tree ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
    expected_tree text NOT NULL CHECK (expected_tree ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
    reason text NOT NULL CHECK (reason IN (
        'verified_two_parent', 'unexpected_merge_observation', 'late_merge_observation',
        'merge_rejected_with_effect', 'tree_mismatch', 'parent_mismatch'
    )),
    observation_digest bytea NOT NULL CHECK (octet_length(observation_digest) = 32),
    observed_at timestamptz NOT NULL,
    UNIQUE (approval_id, dispatch_fence, observation_digest)
);

CREATE TABLE IF NOT EXISTS release_control.broker_switch_operation (
    operation_id uuid PRIMARY KEY,
    approval_id uuid NOT NULL UNIQUE REFERENCES release_control.release_card(id),
    target text NOT NULL REFERENCES release_control.target_queue(target),
    dispatch_fence bigint NOT NULL CHECK (dispatch_fence > 0),
    baseline_identity jsonb NOT NULL CHECK (jsonb_typeof(baseline_identity) = 'object'),
    payload_identity jsonb NOT NULL CHECK (jsonb_typeof(payload_identity) = 'object'),
    payload_digest bytea NOT NULL CHECK (octet_length(payload_digest) = 32),
    state text NOT NULL CHECK (state IN ('prepared', 'admitted', 'started', 'healthy', 'restored_healthy', 'recovery_failed')),
    prepared_at timestamptz NOT NULL,
    admitted_at timestamptz,
    start_deadline timestamptz,
    recovery_deadline timestamptz,
    started_at timestamptz,
    receipt_id text,
    receipt_digest bytea,
    receipt jsonb,
    terminal_at timestamptz,
    CHECK ((admitted_at IS NULL) = (start_deadline IS NULL)),
    CHECK ((admitted_at IS NULL) = (recovery_deadline IS NULL)),
    CHECK (recovery_deadline IS NULL OR recovery_deadline > start_deadline),
    CHECK ((receipt_id IS NULL) = (receipt_digest IS NULL)),
    CHECK ((receipt_id IS NULL) = (receipt IS NULL)),
    CHECK ((receipt_id IS NULL) = (terminal_at IS NULL)),
    CHECK (receipt_digest IS NULL OR octet_length(receipt_digest) = 32),
    CHECK (state NOT IN ('healthy', 'restored_healthy', 'recovery_failed') OR receipt_id IS NOT NULL),
    UNIQUE (target, dispatch_fence, operation_id)
);

CREATE TABLE IF NOT EXISTS release_control.broker_release_revocation (
    update_id bigint PRIMARY KEY CHECK (update_id > 0),
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    target text NOT NULL REFERENCES release_control.target_queue(target),
    owner_user_id bigint NOT NULL CHECK (owner_user_id > 0),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    receipt_id uuid NOT NULL UNIQUE,
    recorded_at timestamptz NOT NULL,
    UNIQUE (approval_id, update_id)
);
CREATE INDEX IF NOT EXISTS broker_release_revocation_approval
    ON release_control.broker_release_revocation (approval_id);

CREATE OR REPLACE FUNCTION release_control.lock_release_revocation_context(p_approval_id uuid)
RETURNS TABLE (target text, owner_user_id bigint, telegram_owner_user_id bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, release_control
AS $$
DECLARE
    v_target text;
BEGIN
    SELECT c.target INTO v_target
      FROM release_control.release_card AS c
     WHERE c.id = p_approval_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM 1 FROM release_control.target_queue AS q
     WHERE q.target = v_target FOR UPDATE;
    PERFORM 1 FROM release_control.release_card AS c
     WHERE c.id = p_approval_id FOR UPDATE;
    PERFORM 1 FROM release_control.broker_target_state AS s
     WHERE s.target = v_target FOR UPDATE;
    RETURN QUERY
        SELECT c.target, c.owner_user_id, s.telegram_owner_user_id
          FROM release_control.release_card AS c
          JOIN release_control.broker_target_state AS s ON s.target = c.target
         WHERE c.id = p_approval_id;
END;
$$;
REVOKE ALL ON FUNCTION release_control.lock_release_revocation_context(uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION release_control.guard_broker_release_revocation_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'release revocations are append-only';
END;
$$;
DROP TRIGGER IF EXISTS broker_release_revocation_append_only
    ON release_control.broker_release_revocation;
CREATE TRIGGER broker_release_revocation_append_only
    BEFORE UPDATE OR DELETE ON release_control.broker_release_revocation
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_broker_release_revocation_append_only();

CREATE OR REPLACE FUNCTION release_control.guard_broker_immutable_rows()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'broker_target_state' THEN
        IF NEW.target IS DISTINCT FROM OLD.target THEN
            RAISE EXCEPTION 'immutable broker target key';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'broker_merge_lease' THEN
        IF ROW(NEW.lease_id, NEW.approval_id, NEW.target, NEW.dispatch_fence,
               NEW.repository_id, NEW.run_id, NEW.run_attempt, NEW.check_run_id,
               NEW.created_at, NEW.expires_at)
           IS DISTINCT FROM
           ROW(OLD.lease_id, OLD.approval_id, OLD.target, OLD.dispatch_fence,
               OLD.repository_id, OLD.run_id, OLD.run_attempt, OLD.check_run_id,
               OLD.created_at, OLD.expires_at) THEN
            RAISE EXCEPTION 'immutable broker merge lease';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'broker_merge_admission' THEN
        RAISE EXCEPTION 'immutable broker merge admission';
    END IF;
    IF TG_TABLE_NAME = 'broker_merge_observation' THEN
        RAISE EXCEPTION 'append only broker merge observation';
    END IF;
    IF TG_TABLE_NAME = 'broker_switch_operation' THEN
        IF ROW(NEW.operation_id, NEW.approval_id, NEW.target, NEW.dispatch_fence,
               NEW.baseline_identity, NEW.payload_identity, NEW.payload_digest, NEW.prepared_at)
           IS DISTINCT FROM
           ROW(OLD.operation_id, OLD.approval_id, OLD.target, OLD.dispatch_fence,
               OLD.baseline_identity, OLD.payload_identity, OLD.payload_digest, OLD.prepared_at)
           OR (OLD.receipt_id IS NOT NULL AND ROW(NEW.receipt_id, NEW.receipt_digest, NEW.receipt,
               NEW.terminal_at) IS DISTINCT FROM ROW(OLD.receipt_id, OLD.receipt_digest,
               OLD.receipt, OLD.terminal_at)) THEN
            RAISE EXCEPTION 'immutable broker switch binding or receipt';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'unexpected broker table';
END;
$$;

DROP TRIGGER IF EXISTS broker_target_state_guard ON release_control.broker_target_state;
CREATE TRIGGER broker_target_state_guard BEFORE UPDATE ON release_control.broker_target_state
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_broker_immutable_rows();
DROP TRIGGER IF EXISTS broker_merge_lease_guard ON release_control.broker_merge_lease;
CREATE TRIGGER broker_merge_lease_guard BEFORE UPDATE ON release_control.broker_merge_lease
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_broker_immutable_rows();
DROP TRIGGER IF EXISTS broker_merge_admission_guard ON release_control.broker_merge_admission;
CREATE TRIGGER broker_merge_admission_guard BEFORE UPDATE ON release_control.broker_merge_admission
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_broker_immutable_rows();
DROP TRIGGER IF EXISTS broker_merge_observation_guard ON release_control.broker_merge_observation;
CREATE TRIGGER broker_merge_observation_guard BEFORE UPDATE OR DELETE ON release_control.broker_merge_observation
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_broker_immutable_rows();
DROP TRIGGER IF EXISTS broker_switch_operation_guard ON release_control.broker_switch_operation;
CREATE TRIGGER broker_switch_operation_guard BEFORE UPDATE ON release_control.broker_switch_operation
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_broker_immutable_rows();

REVOKE ALL ON ALL TABLES IN SCHEMA release_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA release_control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA release_control FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
