-- Unified customer identity auto-attribution.
--
-- External facts (Gmail, Sela, Agent, browser capture) need one deterministic
-- decision about their owning Customer.  Derivable evidence (contact email,
-- website domain, a provider thread that was already attributed, a source's
-- existing customer/external-id link) is recomputed at read time.  This table
-- stores only what cannot be derived: durable, reusable identity facts, mostly
-- written when a human confirms or corrects an attribution.  One active row
-- maps one explicit identifier to exactly one Customer so the same question is
-- never asked twice.  Auto-attribution records its basis; this table never
-- creates a Customer.
BEGIN;

CREATE TABLE IF NOT EXISTS trosa.identity_link_facts (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL DEFAULT trosa.compat_org_id()
        REFERENCES identity.organizations(id),
    legacy_user_id text NOT NULL DEFAULT trosa.compat_current_user(),
    account_id uuid NOT NULL REFERENCES trosa.accounts(id) ON DELETE CASCADE,
    identifier_type text NOT NULL
        CHECK(identifier_type IN ('email', 'domain', 'thread', 'source')),
    identifier_value text NOT NULL,
    origin text NOT NULL DEFAULT 'human_confirmed'
        CHECK(origin IN ('human_confirmed', 'auto', 'imported')),
    method text NOT NULL DEFAULT '',
    resolution text NOT NULL DEFAULT '',
    source_inbox_item_id uuid REFERENCES trosa.inbox_items(id) ON DELETE SET NULL,
    created_by text NOT NULL DEFAULT '',
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Exactly one active fact per identifier for a user.  Revoked rows are history
-- and do not participate in uniqueness, so a later correction can reactivate
-- the same identifier without deleting the audit trail.
CREATE UNIQUE INDEX IF NOT EXISTS trosa_identity_link_facts_active_idx
    ON trosa.identity_link_facts
       (organization_id, legacy_user_id, identifier_type, identifier_value)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS trosa_identity_link_facts_account_idx
    ON trosa.identity_link_facts (account_id, identifier_type);

COMMIT;
