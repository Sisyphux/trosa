-- Follow-up suggestions: what the system works out each morning from the
-- timeline ("this customer has gone quiet since X").
--
-- A suggestion is NOT a task, a proposal or an Inbox item.  It is a private,
-- short-lived note for one member: if nobody confirms it, it expires.  Keeping
-- it in its own table is what stops it from ever piling up as overdue work.
--
--   * scoped by organization + legacy user like every other canonical row;
--     readers always filter on trosa.compat_current_user(), so one member never
--     sees another member's rows;
--   * dedupe_key (owner:account:rule:evidence) is unique per member, so running
--     the daily scan twice never adds rows, and a customer the member said
--     "no need" about stays muted for the same evidence;
--   * evidence_event_id points at the timeline event the reason hangs on.  It is
--     not a foreign key: a deleted event must not delete the audit trail.
--
-- Only adds a table; no existing table is touched.  Forward-only.
BEGIN;

CREATE TABLE IF NOT EXISTS trosa.followup_suggestions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL DEFAULT trosa.compat_org_id()
    REFERENCES identity.organizations(id),
  legacy_user_id text NOT NULL DEFAULT trosa.compat_current_user(),
  account_id uuid NOT NULL REFERENCES trosa.accounts(id),
  suggestion_date date NOT NULL,
  rule text NOT NULL CHECK (rule IN ('R1', 'R2', 'R3', 'R4')),
  certainty text NOT NULL DEFAULT 'hint' CHECK (certainty IN ('certain', 'hint')),
  evidence_event_id uuid,
  priority integer NOT NULL DEFAULT 0,
  reason text NOT NULL,
  facts jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'new'
    CHECK (status IN ('new', 'handled', 'dismissed', 'snoozed', 'expired', 'voided')),
  snooze_until date,
  feedback_reason text,
  handled_at timestamptz,
  dismissed_at timestamptz,
  snoozed_at timestamptz,
  expired_at timestamptz,
  voided_at timestamptz,
  dedupe_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, legacy_user_id, dedupe_key)
);

-- "Today's new suggestions for me" and the daily housekeeping both scan by
-- member + status.
CREATE INDEX IF NOT EXISTS followup_suggestions_member_status_idx
  ON trosa.followup_suggestions (organization_id, legacy_user_id, status, suggestion_date DESC);

CREATE INDEX IF NOT EXISTS followup_suggestions_account_idx
  ON trosa.followup_suggestions (organization_id, legacy_user_id, account_id);

COMMIT;
