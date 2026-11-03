-- Maintenance-installed protected ledger schema. Application startup must never apply this.
-- Execute as the dedicated protected ledger owner; never grant release_control to the bot role.
CREATE SCHEMA IF NOT EXISTS release_control;
REVOKE ALL ON SCHEMA release_control FROM PUBLIC;

CREATE TABLE IF NOT EXISTS release_control.target_queue (
    target text PRIMARY KEY CHECK (target ~ '^[A-Za-z0-9._-]{1,64}$'),
    next_sequence bigint NOT NULL DEFAULT 1 CHECK (next_sequence > 0),
    next_fence bigint NOT NULL DEFAULT 1 CHECK (next_fence > 0),
    active_approval_id uuid,
    active_fence bigint,
    active_until timestamptz,
    CHECK ((active_approval_id IS NULL) = (active_fence IS NULL)),
    CHECK (active_fence IS NULL OR active_fence > 0),
    CHECK (active_approval_id IS NOT NULL OR active_until IS NULL)
);

CREATE TABLE IF NOT EXISTS release_control.release_card (
    id uuid PRIMARY KEY,
    target text NOT NULL REFERENCES release_control.target_queue(target),
    sequence bigint NOT NULL CHECK (sequence > 0),
    status text NOT NULL CHECK (status IN (
        'awaiting_approval', 'rejected', 'expired', 'invalidated', 'queued',
        'merge_authorized', 'merged_verified', 'merged_blocked', 'deploying',
        'healthy', 'restoring', 'restored_healthy', 'recovery_failed'
    )),
    owner_user_id bigint NOT NULL CHECK (owner_user_id > 0),
    chat_id bigint NOT NULL CHECK (chat_id > 0 AND chat_id = owner_user_id),
    message_id bigint NOT NULL CHECK (message_id > 0),
    nonce_sha256 bytea NOT NULL CHECK (octet_length(nonce_sha256) = 32),
    nonce_consumed_at timestamptz,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    binding jsonb NOT NULL,
    binding_sha256 bytea NOT NULL CHECK (octet_length(binding_sha256) = 32),
    decision text CHECK (decision IN ('approve', 'reject')),
    decision_at timestamptz,
    fence bigint,
    run_repository_id bigint CHECK (run_repository_id > 0),
    run_id bigint CHECK (run_id > 0),
    run_attempt integer CHECK (run_attempt > 0),
    workflow_ref text,
    workflow_sha text,
    check_run_id bigint CHECK (check_run_id > 0),
    run_bound_at timestamptz,
    expected_merge_tree text,
    merge_commit text,
    merge_tree text,
    merge_parent_base text,
    merge_parent_head text,
    merge_recorded_at timestamptz,
    merge_anomaly jsonb,
    host_operation_id uuid,
    host_baseline_identity jsonb,
    host_payload_identity jsonb,
    host_payload_sha256 bytea,
    host_receipt_id text,
    host_receipt_sha256 bytea,
    host_receipt jsonb,
    host_recorded_at timestamptz,
    UNIQUE (target, sequence),
    UNIQUE (chat_id, message_id),
    CHECK (expires_at = issued_at + interval '24 hours'),
    CHECK ((decision IS NULL) = (decision_at IS NULL)),
    CHECK ((decision IS NULL) = (nonce_consumed_at IS NULL)),
    CHECK (status <> 'awaiting_approval' OR decision IS NULL),
    CHECK (status <> 'rejected' OR decision IS NOT DISTINCT FROM 'reject'),
    CHECK (status NOT IN (
        'queued', 'merge_authorized', 'merged_verified', 'merged_blocked',
        'deploying', 'healthy', 'restoring', 'restored_healthy', 'recovery_failed'
    ) OR decision IS NOT DISTINCT FROM 'approve'),
    CHECK (
        (run_bound_at IS NULL AND run_repository_id IS NULL AND run_id IS NULL
            AND run_attempt IS NULL AND workflow_ref IS NULL AND workflow_sha IS NULL
            AND check_run_id IS NULL)
        OR
        (run_bound_at IS NOT NULL AND run_repository_id IS NOT NULL AND run_id IS NOT NULL
            AND run_attempt IS NOT NULL AND workflow_ref IS NOT NULL AND workflow_sha IS NOT NULL
            AND check_run_id IS NOT NULL)
    ),
    CHECK (
        (merge_recorded_at IS NULL AND expected_merge_tree IS NULL AND merge_commit IS NULL
            AND merge_tree IS NULL AND merge_parent_base IS NULL AND merge_parent_head IS NULL)
        OR
        (merge_recorded_at IS NOT NULL AND expected_merge_tree IS NOT NULL
            AND merge_commit IS NOT NULL AND merge_tree IS NOT NULL
            AND merge_parent_base IS NOT NULL AND merge_parent_head IS NOT NULL)
    ),
    CHECK (status <> 'merge_authorized' OR run_bound_at IS NOT NULL),
    CHECK (status NOT IN (
        'merged_verified', 'merged_blocked', 'deploying', 'healthy', 'restoring',
        'restored_healthy', 'recovery_failed'
    ) OR merge_recorded_at IS NOT NULL OR (
        status = 'merged_blocked' AND merge_anomaly IS NOT NULL
    )),
    CHECK (merge_anomaly IS NULL OR status = 'merged_blocked'),
    CHECK (merge_anomaly IS NULL OR jsonb_typeof(merge_anomaly) = 'object'),
    CHECK (status <> 'merged_verified' OR (
        merge_commit IS NOT NULL AND merge_commit NOT IN (merge_parent_base, merge_parent_head)
        AND merge_tree = expected_merge_tree
    )),
    CHECK (
        (host_operation_id IS NULL AND host_baseline_identity IS NULL
            AND host_payload_identity IS NULL AND host_payload_sha256 IS NULL)
        OR
        (host_operation_id IS NOT NULL AND host_baseline_identity IS NOT NULL
            AND host_payload_identity IS NOT NULL AND host_payload_sha256 IS NOT NULL
            AND octet_length(host_payload_sha256) = 32)
    ),
    CHECK (
        (host_receipt_id IS NULL AND host_receipt_sha256 IS NULL
            AND host_receipt IS NULL AND host_recorded_at IS NULL)
        OR
        (host_receipt_id IS NOT NULL AND host_operation_id IS NOT NULL
            AND host_receipt_sha256 IS NOT NULL AND octet_length(host_receipt_sha256) = 32
            AND host_receipt IS NOT NULL AND host_recorded_at IS NOT NULL)
    ),
    CHECK (status NOT IN ('deploying', 'restoring') OR host_operation_id IS NOT NULL),
    CHECK (status NOT IN ('healthy', 'restored_healthy', 'recovery_failed')
        OR host_receipt_id IS NOT NULL),
    UNIQUE (run_id, run_attempt),
    UNIQUE (nonce_sha256),
    UNIQUE (host_operation_id),
    UNIQUE (host_receipt_id),
    CHECK (fence IS NULL OR fence > 0)
);

DO $$
BEGIN
    ALTER TABLE release_control.target_queue
        ADD CONSTRAINT target_queue_active_card_fk
        FOREIGN KEY (active_approval_id)
        REFERENCES release_control.release_card(id)
        DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END;
$$;

CREATE TABLE IF NOT EXISTS release_control.telegram_update (
    update_id bigint PRIMARY KEY CHECK (update_id > 0),
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    request_sha256 bytea NOT NULL CHECK (octet_length(request_sha256) = 32),
    result jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS release_control.decision_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    approval_id uuid NOT NULL UNIQUE REFERENCES release_control.release_card(id),
    target text NOT NULL REFERENCES release_control.target_queue(target),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL CHECK (event_type = 'dispatch_release'),
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'leased', 'dispatched', 'canceled')),
    lease_owner text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    dispatched_at timestamptz,
    UNIQUE (target, sequence),
    CHECK (
        (state = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (state <> 'leased' AND lease_owner IS NULL AND lease_until IS NULL)
    ),
    CHECK ((state = 'dispatched') = (dispatched_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS decision_outbox_pending_order
    ON release_control.decision_outbox (target, sequence)
    WHERE state IN ('pending', 'leased');

CREATE TABLE IF NOT EXISTS release_control.release_run_history (
    approval_id uuid NOT NULL REFERENCES release_control.release_card(id),
    dispatch_fence bigint NOT NULL CHECK (dispatch_fence > 0),
    repository_id bigint NOT NULL CHECK (repository_id > 0),
    run_id bigint NOT NULL CHECK (run_id > 0),
    run_attempt integer NOT NULL CHECK (run_attempt > 0),
    workflow_ref text NOT NULL,
    workflow_sha text NOT NULL,
    check_run_id bigint NOT NULL CHECK (check_run_id > 0),
    run_bound_at timestamptz NOT NULL,
    conclusion text NOT NULL CHECK (conclusion IN ('cancelled','failure','timed_out','skipped','action_required','startup_failure')),
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (approval_id,dispatch_fence),
    UNIQUE (repository_id,run_id,run_attempt)
);
CREATE OR REPLACE FUNCTION release_control.guard_run_history()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append only run history'; END;
$$;
DROP TRIGGER IF EXISTS release_run_history_guard ON release_control.release_run_history;
CREATE TRIGGER release_run_history_guard BEFORE UPDATE OR DELETE ON release_control.release_run_history
FOR EACH ROW EXECUTE FUNCTION release_control.guard_run_history();

CREATE OR REPLACE FUNCTION release_control.guard_immutable_release_binding()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.id, NEW.target, NEW.sequence, NEW.owner_user_id, NEW.chat_id,
        NEW.message_id, NEW.nonce_sha256, NEW.issued_at, NEW.expires_at,
        NEW.binding, NEW.binding_sha256
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.target, OLD.sequence, OLD.owner_user_id, OLD.chat_id,
        OLD.message_id, OLD.nonce_sha256, OLD.issued_at, OLD.expires_at,
        OLD.binding, OLD.binding_sha256
    ) THEN
        RAISE EXCEPTION 'immutable release binding';
    END IF;
    IF OLD.decision IS NOT NULL AND NEW.decision IS DISTINCT FROM OLD.decision THEN
        RAISE EXCEPTION 'release decision is immutable';
    END IF;
    IF OLD.run_id IS NOT NULL AND ROW(
        NEW.run_repository_id, NEW.run_id, NEW.run_attempt, NEW.workflow_ref,
        NEW.workflow_sha, NEW.check_run_id, NEW.run_bound_at
    ) IS DISTINCT FROM ROW(
        OLD.run_repository_id, OLD.run_id, OLD.run_attempt, OLD.workflow_ref,
        OLD.workflow_sha, OLD.check_run_id, OLD.run_bound_at
    ) THEN
        IF NOT (
            OLD.status='queued' AND NEW.status='queued' AND NEW.fence > OLD.fence
            AND NEW.run_id IS NULL AND NEW.run_repository_id IS NULL AND NEW.run_attempt IS NULL
            AND NEW.workflow_ref IS NULL AND NEW.workflow_sha IS NULL
            AND NEW.check_run_id IS NULL AND NEW.run_bound_at IS NULL
            AND EXISTS (
                SELECT 1 FROM release_control.release_run_history h
                WHERE h.approval_id=OLD.id AND h.dispatch_fence=OLD.fence
                    AND h.repository_id=OLD.run_repository_id AND h.run_id=OLD.run_id
                    AND h.run_attempt=OLD.run_attempt AND h.workflow_ref=OLD.workflow_ref
                    AND h.workflow_sha=OLD.workflow_sha AND h.check_run_id=OLD.check_run_id
                    AND h.run_bound_at=OLD.run_bound_at
            )
        ) THEN
            RAISE EXCEPTION 'run attempt binding is immutable';
        END IF;
    END IF;
    IF OLD.merge_recorded_at IS NOT NULL AND ROW(
        NEW.expected_merge_tree, NEW.merge_commit, NEW.merge_tree,
        NEW.merge_parent_base, NEW.merge_parent_head, NEW.merge_recorded_at
    ) IS DISTINCT FROM ROW(
        OLD.expected_merge_tree, OLD.merge_commit, OLD.merge_tree,
        OLD.merge_parent_base, OLD.merge_parent_head, OLD.merge_recorded_at
    ) THEN
        RAISE EXCEPTION 'merge result is immutable';
    END IF;
    IF OLD.merge_anomaly IS NOT NULL AND NEW.merge_anomaly IS DISTINCT FROM OLD.merge_anomaly THEN
        RAISE EXCEPTION 'merge anomaly is immutable';
    END IF;
    IF OLD.host_operation_id IS NOT NULL AND ROW(
        NEW.host_operation_id, NEW.host_baseline_identity, NEW.host_payload_identity,
        NEW.host_payload_sha256
    ) IS DISTINCT FROM ROW(
        OLD.host_operation_id, OLD.host_baseline_identity, OLD.host_payload_identity,
        OLD.host_payload_sha256
    ) THEN
        RAISE EXCEPTION 'host operation binding is immutable';
    END IF;
    IF OLD.host_receipt_id IS NOT NULL AND ROW(
        NEW.host_operation_id, NEW.host_receipt_id, NEW.host_receipt_sha256,
        NEW.host_receipt, NEW.host_recorded_at
    ) IS DISTINCT FROM ROW(
        OLD.host_operation_id, OLD.host_receipt_id, OLD.host_receipt_sha256,
        OLD.host_receipt, OLD.host_recorded_at
    ) THEN
        RAISE EXCEPTION 'host receipt is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS release_card_binding_immutable ON release_control.release_card;
CREATE TRIGGER release_card_binding_immutable
    BEFORE UPDATE ON release_control.release_card
    FOR EACH ROW EXECUTE FUNCTION release_control.guard_immutable_release_binding();

REVOKE ALL ON ALL TABLES IN SCHEMA release_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA release_control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA release_control FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA release_control REVOKE ALL ON FUNCTIONS FROM PUBLIC;
-- PostgreSQL adds global defaults to per-schema defaults, so revoke its built-in
-- PUBLIC function execute grant globally for the schema installer role too.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
