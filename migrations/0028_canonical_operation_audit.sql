-- Store the human/API operation journal as a canonical audit fact.
--
-- ``trade_os_compat.operation_logs`` remains an identifier/shape adapter for
-- old clients.  New PostgreSQL runtime code writes this relation directly so
-- an operation is never dependent on the historical SQLite journal.
BEGIN;

CREATE TABLE IF NOT EXISTS audit.operation_log_events (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    legacy_user_id text NOT NULL,
    legacy_id bigint NOT NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id bigint,
    target_reference text NOT NULL DEFAULT '',
    details text NOT NULL DEFAULT '',
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, legacy_user_id, legacy_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS audit_operation_log_events_org_user_time_idx
    ON audit.operation_log_events (organization_id, legacy_user_id, occurred_at DESC, legacy_id DESC);

COMMIT;
