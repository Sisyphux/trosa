-- Inbox dialogue backend: a real text conversation between the user and sela.
--
-- The old Inbox stored one row per "question" and answered it with a
-- type-driven form.  This migration introduces the storage the frozen
-- inbox-dialogue-contract describes: threads, their messages, the receipts
-- that make every write idempotent, and the counters that prove when the old
-- routes are safe to retire.
--
-- Design decisions (see sela docs/proposals/inbox-dialogue-contract.md):
--   * ``subject`` is a structured object id ("prospect:<source_id>"), never a
--     question type.  The partial unique index keeps at most one OPEN thread
--     per subject so a create race collapses onto one thread.
--   * ``revision`` is bumped by every state change in the same transaction as
--     the message write; callers pass ``seen_revision`` and lose a 409 on a
--     stale value.
--   * ``inbox_action_receipts`` is scoped by organization + legacy user like
--     every other canonical row.  The partial unique index on
--     ``consumed_message_id`` is the sole arbiter of "a confirmation message
--     may be consumed exactly once", so two concurrent irreversible actions
--     cannot both win.
--   * ``inbox_legacy_route_hits`` only counts calls.  It carries no business
--     state and can be dropped once the old routes are retired.
--
-- Forward-only: re-running is safe (IF NOT EXISTS everywhere).
BEGIN;

CREATE TABLE IF NOT EXISTS trosa.inbox_threads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL DEFAULT trosa.compat_org_id()
    REFERENCES identity.organizations(id),
  legacy_user_id text NOT NULL DEFAULT trosa.compat_current_user(),
  subject text,
  title text NOT NULL,
  status text NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'closed')),
  awaiting text NOT NULL DEFAULT 'human'
    CHECK (awaiting IN ('human', 'sela', 'none')),
  revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
  opened_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz,
  closed_by text CHECK (closed_by IN ('human', 'sela', 'system')),
  closed_summary text
);

-- At most one open thread per (org, user, subject).  Threads without a
-- subject are intentionally allowed to repeat (they are not object-bound).
CREATE UNIQUE INDEX IF NOT EXISTS inbox_threads_open_subject_idx
  ON trosa.inbox_threads (organization_id, legacy_user_id, subject)
  WHERE status = 'open' AND subject IS NOT NULL;

-- The "waiting for sela" queue and the observability counts both scan open
-- threads ordered by recency.
CREATE INDEX IF NOT EXISTS inbox_threads_awaiting_idx
  ON trosa.inbox_threads (organization_id, legacy_user_id, awaiting, updated_at DESC)
  WHERE status = 'open';

CREATE TABLE IF NOT EXISTS trosa.inbox_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id uuid NOT NULL REFERENCES trosa.inbox_threads(id) ON DELETE CASCADE,
  seq integer NOT NULL CHECK (seq >= 1),
  role text NOT NULL CHECK (role IN ('sela', 'human', 'system')),
  actor text,
  text text NOT NULL,
  suggested_replies jsonb NOT NULL DEFAULT '[]'::jsonb,
  refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  hints jsonb,
  attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
  awaiting_after text CHECK (awaiting_after IN ('human', 'sela', 'none')),
  idempotency_key text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (thread_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS inbox_messages_idem_idx
  ON trosa.inbox_messages (thread_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS trosa.inbox_action_receipts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL DEFAULT trosa.compat_org_id()
    REFERENCES identity.organizations(id),
  legacy_user_id text NOT NULL DEFAULT trosa.compat_current_user(),
  thread_id uuid NOT NULL REFERENCES trosa.inbox_threads(id) ON DELETE CASCADE,
  operation text NOT NULL,
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  response jsonb NOT NULL DEFAULT '{}'::jsonb,
  message_id uuid,
  consumed_message_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, legacy_user_id, operation, idempotency_key)
);

-- The single arbiter of "this confirmation message has been used".  A second
-- irreversible action that names the same confirmation cannot insert a second
-- receipt, so its whole transaction (business write included) rolls back.
CREATE UNIQUE INDEX IF NOT EXISTS inbox_receipts_consumed_idx
  ON trosa.inbox_action_receipts (organization_id, legacy_user_id, consumed_message_id)
  WHERE consumed_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS trosa.inbox_legacy_route_hits (
  organization_id uuid NOT NULL DEFAULT trosa.compat_org_id()
    REFERENCES identity.organizations(id),
  legacy_user_id text NOT NULL DEFAULT trosa.compat_current_user(),
  route text NOT NULL,
  day date NOT NULL,
  hits integer NOT NULL DEFAULT 0,
  PRIMARY KEY (organization_id, legacy_user_id, route, day)
);

COMMIT;
