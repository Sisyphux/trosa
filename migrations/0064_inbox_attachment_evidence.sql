-- Auditable metadata for files already stored through the governed customer
-- attachment service.  Binary storage remains core.file_objects.
CREATE TABLE IF NOT EXISTS trosa.inbox_attachment_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES core.organizations(id),
  question_key text NOT NULL,
  account_id uuid NOT NULL REFERENCES core.accounts(id),
  file_object_id uuid NOT NULL REFERENCES core.file_objects(id),
  purpose text NOT NULL DEFAULT 'investigation',
  analysis_status text NOT NULL DEFAULT 'uploaded',
  extraction_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  conclusion_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  uploaded_by text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, question_key, file_object_id)
);
