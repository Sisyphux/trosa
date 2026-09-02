-- Trade OS unified PostgreSQL foundation.
--
-- This migration creates one business data base.  Schemas are module
-- boundaries only: no table here represents a second Trosa or sela database.
-- IDs are supplied by the application/migration worker as UUIDs so this file
-- does not require a database extension to generate them.

BEGIN;

CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS trosa;
CREATE SCHEMA IF NOT EXISTS sela;
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS identity.organizations (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity.users (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    legacy_user_id text,
    auth_subject text,
    display_name text NOT NULL,
    label text NOT NULL DEFAULT '',
    color text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, legacy_user_id),
    UNIQUE (organization_id, auth_subject)
);

CREATE TABLE IF NOT EXISTS identity.memberships (
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    user_id uuid NOT NULL REFERENCES identity.users(id),
    role text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);

CREATE TABLE IF NOT EXISTS identity.settings (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    user_id uuid REFERENCES identity.users(id),
    setting_key text NOT NULL,
    value jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (organization_id, user_id, setting_key)
);

CREATE TABLE IF NOT EXISTS core.companies (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    canonical_name text NOT NULL,
    normalized_name text NOT NULL,
    website text NOT NULL DEFAULT '',
    country_code text NOT NULL DEFAULT '',
    city text NOT NULL DEFAULT '',
    business_type text NOT NULL DEFAULT '',
    identity_status text NOT NULL DEFAULT 'confirmed',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS core_companies_name_country_idx
    ON core.companies (organization_id, normalized_name, country_code);

CREATE TABLE IF NOT EXISTS core.company_domains (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    normalized_domain text NOT NULL,
    source_url text NOT NULL DEFAULT '',
    is_primary boolean NOT NULL DEFAULT false,
    verification_status text NOT NULL DEFAULT 'unverified',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, normalized_domain)
);

CREATE UNIQUE INDEX IF NOT EXISTS core_company_primary_domain_idx
    ON core.company_domains (normalized_domain) WHERE is_primary;

CREATE TABLE IF NOT EXISTS core.company_aliases (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    alias text NOT NULL,
    normalized_alias text NOT NULL,
    source text NOT NULL DEFAULT '',
    confidence text NOT NULL DEFAULT 'unknown',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS core.people (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    full_name text NOT NULL,
    normalized_name text NOT NULL DEFAULT '',
    identity_status text NOT NULL DEFAULT 'confirmed',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.company_people (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    person_id uuid NOT NULL REFERENCES core.people(id),
    title text NOT NULL DEFAULT '',
    department text NOT NULL DEFAULT '',
    relationship_status text NOT NULL DEFAULT 'current',
    source text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, person_id, title)
);

CREATE TABLE IF NOT EXISTS core.contact_methods (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    company_id uuid REFERENCES core.companies(id),
    person_id uuid REFERENCES core.people(id),
    kind text NOT NULL,
    value text NOT NULL,
    normalized_value text NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    verification_status text NOT NULL DEFAULT 'unknown',
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(company_id, person_id) = 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS core_contact_method_unique_idx
    ON core.contact_methods (organization_id, kind, normalized_value);

CREATE TABLE IF NOT EXISTS core.email_verification_observations (
    id uuid PRIMARY KEY,
    contact_method_id uuid NOT NULL REFERENCES core.contact_methods(id),
    status text NOT NULL,
    confidence text NOT NULL DEFAULT '',
    risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    checked_at timestamptz,
    expires_at timestamptz,
    source text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.accounts (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    company_id uuid NOT NULL REFERENCES core.companies(id),
    owner_user_id uuid REFERENCES identity.users(id),
    display_name text NOT NULL DEFAULT '',
    account_status text NOT NULL DEFAULT '',
    customer_type text NOT NULL DEFAULT '',
    channel_type text NOT NULL DEFAULT '',
    priority_level text NOT NULL DEFAULT '',
    profile text NOT NULL DEFAULT '',
    field text NOT NULL DEFAULT '',
    industry text NOT NULL DEFAULT '',
    company_size text NOT NULL DEFAULT '',
    annual_revenue text NOT NULL DEFAULT '',
    tags text NOT NULL DEFAULT '',
    attention_state text NOT NULL DEFAULT '',
    attention_reason text NOT NULL DEFAULT '',
    attention_updated_at timestamptz,
    attention_review_date timestamptz,
    last_contact_at timestamptz,
    next_follow_up_at timestamptz,
    is_pinned boolean NOT NULL DEFAULT false,
    pinned_order integer NOT NULL DEFAULT 0,
    deleted_at timestamptz,
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, company_id)
);

CREATE TABLE IF NOT EXISTS trosa.tasks (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    assigned_to_user_id uuid REFERENCES identity.users(id),
    title text NOT NULL DEFAULT '',
    content text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    due_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'open',
    task_type text NOT NULL DEFAULT 'follow_up',
    source_activity_legacy_id text NOT NULL DEFAULT '',
    manual_order integer NOT NULL DEFAULT 0,
    completed_at timestamptz,
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.timeline_events (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    contact_method_id uuid REFERENCES core.contact_methods(id),
    event_type text NOT NULL,
    direction text NOT NULL DEFAULT 'unknown',
    content text NOT NULL DEFAULT '',
    result text NOT NULL DEFAULT '',
    next_plan text NOT NULL DEFAULT '',
    source_module text NOT NULL DEFAULT 'trosa',
    source_reference text NOT NULL DEFAULT '',
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trosa_timeline_account_time_idx
    ON trosa.timeline_events (account_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS trosa.outreach_messages (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    contact_method_id uuid REFERENCES core.contact_methods(id),
    subject text NOT NULL DEFAULT '',
    body text NOT NULL DEFAULT '',
    sent_at timestamptz,
    reply_status text NOT NULL DEFAULT 'pending',
    reply_content text NOT NULL DEFAULT '',
    reply_at timestamptz,
    provider text NOT NULL DEFAULT '',
    provider_message_id text NOT NULL DEFAULT '',
    provider_thread_id text NOT NULL DEFAULT '',
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.inbox_items (
    id uuid PRIMARY KEY,
    account_id uuid REFERENCES trosa.accounts(id),
    item_type text NOT NULL,
    title text NOT NULL,
    content text NOT NULL DEFAULT '',
    dedupe_key text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'open',
    snoozed_until timestamptz,
    resolved_at timestamptz,
    resolution_reason text NOT NULL DEFAULT '',
    resolution_note text NOT NULL DEFAULT '',
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS trosa_inbox_dedupe_idx
    ON trosa.inbox_items (dedupe_key) WHERE dedupe_key <> '';

CREATE TABLE IF NOT EXISTS trosa.communication_sources (
    id uuid PRIMARY KEY,
    timeline_event_id uuid NOT NULL UNIQUE REFERENCES trosa.timeline_events(id),
    channel text NOT NULL,
    source_url text NOT NULL DEFAULT '',
    account text NOT NULL DEFAULT '',
    conversation_identity text NOT NULL DEFAULT '',
    adapter_version text NOT NULL DEFAULT '',
    extraction_scope text NOT NULL DEFAULT '',
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    raw_payload jsonb NOT NULL,
    cleaned_payload text NOT NULL DEFAULT '',
    captured_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.communication_source_items (
    id uuid PRIMARY KEY,
    communication_source_id uuid NOT NULL REFERENCES trosa.communication_sources(id),
    source_fingerprint text NOT NULL UNIQUE,
    message_time timestamptz,
    direction text NOT NULL DEFAULT 'unknown',
    raw_text text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.email_message_receipts (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    provider_message_id text NOT NULL,
    provider_thread_id text NOT NULL DEFAULT '',
    message_time timestamptz,
    sender_email text NOT NULL DEFAULT '',
    recipient_emails jsonb NOT NULL DEFAULT '[]'::jsonb,
    subject text NOT NULL DEFAULT '',
    account_id uuid REFERENCES trosa.accounts(id),
    contact_method_id uuid REFERENCES core.contact_methods(id),
    timeline_event_id uuid REFERENCES trosa.timeline_events(id),
    inbox_item_id uuid REFERENCES trosa.inbox_items(id),
    match_status text NOT NULL DEFAULT 'unmatched',
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, provider_message_id)
);

CREATE TABLE IF NOT EXISTS trosa.email_delivery_events (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    contact_method_id uuid REFERENCES core.contact_methods(id),
    outreach_message_id uuid REFERENCES trosa.outreach_messages(id),
    event_type text NOT NULL,
    smtp_code text NOT NULL DEFAULT '',
    enhanced_status text NOT NULL DEFAULT '',
    diagnostic_text text NOT NULL DEFAULT '',
    remote_mta text NOT NULL DEFAULT '',
    provider_message_id text NOT NULL DEFAULT '',
    source text NOT NULL DEFAULT 'manual',
    occurred_at timestamptz NOT NULL,
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS trosa.research_reports (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL UNIQUE REFERENCES trosa.accounts(id),
    summary text NOT NULL DEFAULT '',
    company_info text NOT NULL DEFAULT '',
    key_findings text NOT NULL DEFAULT '',
    needs_analysis text NOT NULL DEFAULT '',
    cooperation_value text NOT NULL DEFAULT '',
    raw_input text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.external_analysis_notes (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    content text NOT NULL,
    source text NOT NULL DEFAULT 'external_model',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.account_understandings (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL UNIQUE REFERENCES trosa.accounts(id),
    current_summary text NOT NULL DEFAULT '',
    recent_change text NOT NULL DEFAULT '',
    open_loops jsonb NOT NULL DEFAULT '[]'::jsonb,
    action_state text NOT NULL DEFAULT 'hold',
    action_reason text NOT NULL DEFAULT '',
    source_timeline_event_id uuid REFERENCES trosa.timeline_events(id),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.ai_recommendations (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    understanding_version integer NOT NULL DEFAULT 0,
    content text NOT NULL,
    reason text NOT NULL DEFAULT '',
    source_timeline_event_id uuid REFERENCES trosa.timeline_events(id),
    review_status text NOT NULL DEFAULT 'hold',
    user_response text NOT NULL DEFAULT '',
    user_modified_content text NOT NULL DEFAULT '',
    executed_action text NOT NULL DEFAULT '',
    outcome text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trosa.web_monitor_observations (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    url text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'ok',
    content_hash text NOT NULL DEFAULT '',
    content_snippet text NOT NULL DEFAULT '',
    change_summary text NOT NULL DEFAULT '',
    checked_at timestamptz NOT NULL,
    task_id uuid REFERENCES trosa.tasks(id),
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS core.file_objects (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    storage_key text NOT NULL,
    original_name text NOT NULL,
    mime_type text NOT NULL DEFAULT '',
    size_bytes bigint NOT NULL DEFAULT 0,
    sha256 text NOT NULL DEFAULT '',
    uploaded_by_user_id uuid REFERENCES identity.users(id),
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, storage_key)
);

CREATE TABLE IF NOT EXISTS core.entity_files (
    id uuid PRIMARY KEY,
    file_object_id uuid NOT NULL REFERENCES core.file_objects(id),
    account_id uuid REFERENCES trosa.accounts(id),
    company_id uuid REFERENCES core.companies(id),
    prospect_id uuid,
    relation_type text NOT NULL DEFAULT 'attachment',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(account_id, company_id, prospect_id) = 1)
);

CREATE TABLE IF NOT EXISTS sela.search_runs (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    legacy_run_id text NOT NULL,
    status text NOT NULL DEFAULT '',
    started_at timestamptz,
    completed_at timestamptz,
    manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, legacy_run_id)
);

CREATE TABLE IF NOT EXISTS sela.prospects (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    company_id uuid NOT NULL REFERENCES core.companies(id),
    contact_method_id uuid REFERENCES core.contact_methods(id),
    legacy_candidate_id text NOT NULL,
    campaign text NOT NULL DEFAULT '',
    source_run_id text NOT NULL DEFAULT '',
    qualification_status text NOT NULL DEFAULT '',
    research_status text NOT NULL DEFAULT '',
    confidence text NOT NULL DEFAULT '',
    do_not_contact boolean NOT NULL DEFAULT false,
    imported_at timestamptz,
    updated_at timestamptz,
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, legacy_candidate_id)
);

CREATE INDEX IF NOT EXISTS sela_prospects_company_idx ON sela.prospects (company_id);

CREATE TABLE IF NOT EXISTS sela.prospect_research (
    prospect_id uuid PRIMARY KEY REFERENCES sela.prospects(id),
    qualification_method text NOT NULL DEFAULT '',
    qualification_reason text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    angle text NOT NULL DEFAULT '',
    supplier_pivot text NOT NULL DEFAULT '',
    site_hygiene text NOT NULL DEFAULT '',
    research_reason text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sela.prospect_evidence (
    id uuid PRIMARY KEY,
    prospect_id uuid NOT NULL REFERENCES sela.prospects(id),
    evidence_type text NOT NULL,
    source_url text NOT NULL DEFAULT '',
    source_file text NOT NULL DEFAULT '',
    excerpt text NOT NULL DEFAULT '',
    captured_at timestamptz,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sela.outreach_messages (
    id uuid PRIMARY KEY,
    prospect_id uuid NOT NULL REFERENCES sela.prospects(id),
    subject text NOT NULL DEFAULT '',
    body text NOT NULL DEFAULT '',
    message_variant text NOT NULL DEFAULT '',
    provider text NOT NULL DEFAULT 'gmail',
    provider_draft_id text NOT NULL DEFAULT '',
    provider_thread_id text NOT NULL DEFAULT '',
    provider_message_id text NOT NULL DEFAULT '',
    sent_at timestamptz,
    last_send_attempt_at timestamptz,
    last_send_error text NOT NULL DEFAULT '',
    auto_send_blocked boolean NOT NULL DEFAULT false,
    legacy_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sela.prospect_events (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    prospect_id uuid REFERENCES sela.prospects(id),
    legacy_candidate_id text NOT NULL DEFAULT '',
    occurred_at timestamptz NOT NULL,
    event_type text NOT NULL,
    company_text text NOT NULL DEFAULT '',
    campaign text NOT NULL DEFAULT '',
    market text NOT NULL DEFAULT '',
    business_type text NOT NULL DEFAULT '',
    confidence text NOT NULL DEFAULT '',
    contact_route text NOT NULL DEFAULT '',
    email_type text NOT NULL DEFAULT '',
    email_evidence_tier text NOT NULL DEFAULT '',
    message_variant text NOT NULL DEFAULT '',
    outreach_status_snapshot text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    legacy_row_number integer NOT NULL,
    legacy_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, legacy_row_number)
);

CREATE INDEX IF NOT EXISTS sela_prospect_events_time_idx
    ON sela.prospect_events (prospect_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS sela.run_activity_events (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    legacy_activity_id bigint NOT NULL,
    run_id text NOT NULL DEFAULT '',
    campaign_id text NOT NULL DEFAULT '',
    legacy_candidate_id text NOT NULL DEFAULT '',
    prospect_id uuid REFERENCES sela.prospects(id),
    kind text NOT NULL,
    status text NOT NULL,
    message text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    business_progress boolean NOT NULL DEFAULT false,
    occurred_at timestamptz NOT NULL,
    UNIQUE (organization_id, legacy_activity_id)
);

CREATE TABLE IF NOT EXISTS sela.search_memory_entries (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    entry_type text NOT NULL,
    legacy_row_number integer NOT NULL,
    run_id text NOT NULL DEFAULT '',
    occurred_at timestamptz,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, entry_type, legacy_row_number)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'core_entity_files_prospect_fk'
          AND conrelid = 'core.entity_files'::regclass
    ) THEN
        ALTER TABLE core.entity_files
            ADD CONSTRAINT core_entity_files_prospect_fk
            FOREIGN KEY (prospect_id) REFERENCES sela.prospects(id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS audit.import_batches (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    source_name text NOT NULL,
    source_path text NOT NULL,
    source_sha256 text NOT NULL,
    source_rows integer NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, source_path, source_sha256)
);

CREATE TABLE IF NOT EXISTS audit.legacy_records (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES audit.import_batches(id),
    source_table text NOT NULL,
    legacy_key text NOT NULL,
    legacy_row_number integer,
    payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, source_table, legacy_key)
);

CREATE TABLE IF NOT EXISTS audit.migration_issues (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES audit.import_batches(id),
    severity text NOT NULL,
    issue_code text NOT NULL,
    source_table text NOT NULL,
    legacy_key text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit.events (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    actor_user_id uuid REFERENCES identity.users(id),
    actor_type text NOT NULL,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid,
    request_id text NOT NULL DEFAULT '',
    before_payload jsonb,
    after_payload jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit.integration_receipts (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    integration text NOT NULL,
    idempotency_key text NOT NULL,
    request_sha256 text NOT NULL,
    legacy_candidate_id text NOT NULL DEFAULT '',
    account_id uuid REFERENCES trosa.accounts(id),
    response_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, integration, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit.agent_proposals (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    account_id uuid NOT NULL REFERENCES trosa.accounts(id),
    proposal_type text NOT NULL,
    payload jsonb NOT NULL,
    proposal_action text NOT NULL DEFAULT '',
    source text NOT NULL DEFAULT '',
    source_reference text NOT NULL DEFAULT '',
    idempotency_key text NOT NULL DEFAULT '',
    request_sha256 text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz
);

CREATE TABLE IF NOT EXISTS audit.agent_actions (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    action_id text NOT NULL,
    token_id text NOT NULL,
    actor_user_id uuid REFERENCES identity.users(id),
    action_type text NOT NULL,
    account_id uuid REFERENCES trosa.accounts(id),
    related_type text NOT NULL DEFAULT '',
    related_id text NOT NULL DEFAULT '',
    undo_token text NOT NULL,
    request_payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'completed',
    created_at timestamptz NOT NULL DEFAULT now(),
    undone_at timestamptz,
    UNIQUE (organization_id, action_id)
);

CREATE TABLE IF NOT EXISTS audit.undo_snapshots (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    token text NOT NULL,
    operation text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    entities jsonb NOT NULL,
    status text NOT NULL DEFAULT 'available',
    created_at timestamptz NOT NULL DEFAULT now(),
    undone_at timestamptz,
    UNIQUE (organization_id, token)
);

COMMIT;
