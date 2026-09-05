"""The PostgreSQL relation contract shared by startup and migration checks.

The application uses a deliberately broad compatibility surface while the
canonical data lives in the identity/core/trosa/sela/audit schemas.  Keeping
the contract in one module prevents the runtime readiness check and the
operator-facing verifier from slowly drifting apart.
"""

from __future__ import annotations

from typing import Any


REQUIRED_SCHEMAS = (
    "identity", "core", "trosa", "sela", "audit", "trade_os_compat",
)

REQUIRED_TABLES = (
    "audit.schema_migrations",
    "identity.organizations", "identity.users", "identity.memberships", "identity.settings",
    "identity.team_invitations",
    "core.companies", "core.company_domains", "core.company_aliases", "core.people",
    "core.company_people", "core.contact_methods", "core.email_verification_observations",
    "core.file_objects", "core.entity_files",
    "trosa.accounts", "trosa.tasks", "trosa.timeline_events", "trosa.outreach_messages",
    "trosa.inbox_items", "trosa.communication_sources", "trosa.communication_source_items",
    "trosa.email_message_receipts", "trosa.email_delivery_events", "trosa.research_reports",
    "trosa.external_analysis_notes", "trosa.account_understandings", "trosa.ai_recommendations",
    "trosa.web_monitor_observations", "trosa.account_legacy_refs", "trosa.legacy_row_refs",
    "trosa.contact_legacy_refs", "trosa.weekly_reports", "trosa.email_verifications",
    "trosa.email_verification_jobs", "trosa.email_domain_probes", "trosa.email_logs",
    "sela.search_runs", "sela.prospects", "sela.prospect_research", "sela.prospect_evidence",
    "sela.outreach_messages", "sela.prospect_events", "sela.run_activity_events",
    "sela.search_memory_entries",
    "audit.import_batches", "audit.legacy_records", "audit.migration_issues", "audit.events",
    "audit.integration_receipts", "audit.agent_proposals", "audit.agent_actions",
    "audit.undo_snapshots", "audit.agent_gateway_idempotency", "audit.imported_activity_rows",
    "audit.import_unmatched_customers",
    "trade_os_compat.app_settings", "trade_os_compat.customer_file_rows",
    "trade_os_compat.integration_sync_receipt_rows", "trade_os_compat.operation_log_rows",
    "trade_os_compat.agent_proposal_rows", "trade_os_compat.agent_gateway_rows",
    "trade_os_compat.agent_action_rows", "trade_os_compat.undo_action_rows",
    "trade_os_compat.import_batch_rows", "trade_os_compat.imported_activity_row_rows",
    "trade_os_compat.import_unmatched_customer_rows", "trade_os_compat.email_delivery_event_rows",
    "trade_os_compat.gmail_message_state_rows", "trade_os_compat.communication_source_rows",
    "trade_os_compat.communication_source_item_rows",
)

REQUIRED_VIEWS = (
    "trosa.users", "trosa.customers", "trosa.contacts", "trosa.reminders",
    "trosa.follow_up_logs", "trosa.outreach_emails",
    "trade_os_compat.users", "trade_os_compat.customers", "trade_os_compat.contacts",
    "trade_os_compat.reminders", "trade_os_compat.follow_up_logs", "trade_os_compat.outreach_emails",
    "trade_os_compat.research_reports", "trade_os_compat.external_analysis_notes",
    "trade_os_compat.customer_understandings", "trade_os_compat.ai_recommendations",
    "trade_os_compat.inbox_items", "trade_os_compat.web_monitor_logs", "trade_os_compat.customer_files",
    "trade_os_compat.operation_logs", "trade_os_compat.agent_proposals",
    "trade_os_compat.agent_gateway_idempotency", "trade_os_compat.agent_actions",
    "trade_os_compat.undo_actions", "trade_os_compat.import_batches",
    "trade_os_compat.imported_activity_rows", "trade_os_compat.import_unmatched_customers",
    "trade_os_compat.email_delivery_events", "trade_os_compat.gmail_message_states",
    "trade_os_compat.communication_sources", "trade_os_compat.communication_source_items",
    "trade_os_compat.weekly_reports", "trade_os_compat.email_verifications",
    "trade_os_compat.email_verification_jobs", "trade_os_compat.email_domain_probes",
    "trade_os_compat.email_logs", "trade_os_compat.integration_sync_receipts",
    "trade_os_compat.team_invitations",
)

# These columns were referenced by the compatibility layer before they were
# present in the original foundation migration.  Keep them explicit so a
# table that happens to exist with an old shape is not reported as healthy.
REQUIRED_COLUMNS = (
    ("identity.users", "username"),
    ("identity.users", "password_hash"),
    ("identity.users", "role"),
    ("identity.users", "created_by"),
    ("identity.users", "active"),
    ("identity.users", "legacy_payload"),
    ("trosa.research_reports", "source"),
    ("trosa.research_reports", "web_content"),
    ("trosa.research_reports", "web_fetched_at"),
    ("trosa.research_reports", "expires_at"),
    ("trosa.email_message_receipts", "legacy_user_id"),
    ("trosa.email_verifications", "legacy_id"),
    ("trosa.email_verification_jobs", "legacy_id"),
    ("trosa.email_domain_probes", "legacy_id"),
    ("trosa.email_logs", "legacy_key"),
    ("trosa.communication_source_items", "organization_id"),
    ("trosa.communication_source_items", "legacy_user_id"),
    ("audit.agent_actions", "legacy_user_id"),
    ("audit.undo_snapshots", "legacy_user_id"),
    ("sela.prospect_events", "source_batch_id"),
)

# ``CREATE UNIQUE INDEX IF NOT EXISTS`` only checks the index name.  A
# partially applied rehearsal can therefore keep an old, weaker index under
# the same name while every table/view/function check still passes.  Keep the
# column-level contract explicit for the keys that enforce mailbox and
# browser-capture isolation.
REQUIRED_INDEXES = (
    ("identity.users", "identity_users_username_idx", ("organization_id", "username")),
    ("trosa.email_message_receipts", "trosa_email_receipts_org_user_provider_idx",
     ("organization_id", "legacy_user_id", "provider_message_id")),
    ("trosa.email_verifications", "trosa_email_verification_legacy_idx",
     ("organization_id", "legacy_user_id", "legacy_id")),
    ("trosa.email_verification_jobs", "trosa_email_verification_job_legacy_idx",
     ("organization_id", "legacy_user_id", "legacy_id")),
    ("trosa.email_domain_probes", "trosa_email_domain_probe_legacy_idx",
     ("organization_id", "legacy_user_id", "legacy_id")),
    ("trosa.email_logs", "trosa_email_logs_legacy_key_idx",
     ("organization_id", "legacy_user_id", "legacy_key")),
    ("trosa.communication_source_items", "trosa_communication_items_org_user_fp_idx",
     ("organization_id", "legacy_user_id", "source_fingerprint")),
    ("audit.agent_actions", "audit_agent_actions_org_user_action_idx",
     ("organization_id", "legacy_user_id", "action_id")),
    ("audit.undo_snapshots", "audit_undo_snapshots_org_user_token_idx",
     ("organization_id", "legacy_user_id", "token")),
)

REQUIRED_FUNCTIONS = (
    "trosa.compat_current_user()",
    "trosa.compat_normalized_name(text)",
    "trosa.compat_domain(text)",
    "trosa.compat_uuid(text)",
    "trosa.compat_org_id()",
    "trosa.compat_jsonb(text,jsonb)",
    "trosa.compat_next_id(text,text)",
    "trosa.compat_customer_account(bigint,text)",
    "trosa.compat_set_lastrowid(bigint)",
    "trosa.compat_time(text)",
    "trosa.compat_legacy_bigint(text)",
    "trosa.compat_customers_write()",
    "trosa.compat_contacts_write()",
    "trosa.compat_reminders_write()",
    "trosa.compat_follow_up_write()",
    "trosa.compat_outreach_write()",
    "trade_os_compat.users_write()",
    "trade_os_compat.research_reports_write()",
    "trade_os_compat.external_analysis_notes_write()",
    "trade_os_compat.customer_understandings_write()",
    "trade_os_compat.ai_recommendations_write()",
    "trade_os_compat.inbox_items_write()",
    "trade_os_compat.web_monitor_logs_write()",
    "trade_os_compat.customer_files_write()",
    "trade_os_compat.operation_logs_write()",
    "trade_os_compat.agent_proposals_write()",
    "trade_os_compat.agent_gateway_write()",
    "trade_os_compat.agent_actions_write()",
    "trade_os_compat.undo_actions_write()",
    "trade_os_compat.email_verifications_write()",
    "trade_os_compat.email_verification_jobs_write()",
    "trade_os_compat.email_domain_probes_write()",
    "trade_os_compat.email_logs_write()",
    "trade_os_compat.gmail_message_states_write()",
    "trade_os_compat.communication_sources_write()",
    "trade_os_compat.communication_source_items_write()",
    "trade_os_compat.email_delivery_events_write()",
    "trade_os_compat.weekly_reports_write()",
    "trade_os_compat.import_batches_write()",
    "trade_os_compat.imported_activity_rows_write()",
    "trade_os_compat.import_unmatched_customers_write()",
    "trade_os_compat.integration_sync_receipts_write()",
    "trade_os_compat.team_invitations_write()",
)

REQUIRED_TRIGGERS = (
    "trosa.customers.compat_customers_write",
    "trosa.contacts.compat_contacts_write",
    "trosa.reminders.compat_reminders_write",
    "trosa.follow_up_logs.compat_follow_up_write",
    "trosa.outreach_emails.compat_outreach_write",
    "trade_os_compat.customers.compat_customers_bridge",
    "trade_os_compat.contacts.compat_contacts_bridge",
    "trade_os_compat.reminders.compat_reminders_bridge",
    "trade_os_compat.follow_up_logs.compat_follow_up_bridge",
    "trade_os_compat.outreach_emails.compat_outreach_bridge",
    "trade_os_compat.research_reports.research_reports_write",
    "trade_os_compat.external_analysis_notes.external_analysis_notes_write",
    "trade_os_compat.customer_understandings.customer_understandings_write",
    "trade_os_compat.ai_recommendations.ai_recommendations_write",
    "trade_os_compat.inbox_items.inbox_items_write",
    "trade_os_compat.web_monitor_logs.web_monitor_logs_write",
    "trade_os_compat.customer_files.customer_files_write",
    "trade_os_compat.operation_logs.operation_logs_write",
    "trade_os_compat.agent_proposals.agent_proposals_write",
    "trade_os_compat.agent_gateway_idempotency.agent_gateway_write",
    "trade_os_compat.agent_actions.agent_actions_write",
    "trade_os_compat.undo_actions.undo_actions_write",
    "trade_os_compat.users.users_write",
    "trade_os_compat.email_verifications.email_verifications_write",
    "trade_os_compat.email_verification_jobs.email_verification_jobs_write",
    "trade_os_compat.email_domain_probes.email_domain_probes_write",
    "trade_os_compat.email_logs.email_logs_write",
    "trade_os_compat.gmail_message_states.gmail_message_states_write",
    "trade_os_compat.communication_sources.communication_sources_write",
    "trade_os_compat.communication_source_items.communication_source_items_write",
    "trade_os_compat.email_delivery_events.email_delivery_events_write",
    "trade_os_compat.weekly_reports.weekly_reports_write",
    "trade_os_compat.import_batches.import_batches_write",
    "trade_os_compat.imported_activity_rows.imported_activity_rows_write",
    "trade_os_compat.import_unmatched_customers.import_unmatched_customers_write",
    "trade_os_compat.integration_sync_receipts.integration_sync_receipts_write",
    "trade_os_compat.team_invitations.team_invitations_write",
)


def schema_status(cursor: Any) -> dict[str, Any]:
    """Return object-by-object readiness, not just a single table sentinel."""
    cursor.execute(
        """SELECT schema_name FROM information_schema.schemata
           WHERE schema_name = ANY(%s)""",
        (list(REQUIRED_SCHEMAS),),
    )
    schemas = {row[0] for row in cursor.fetchall()}

    expected_relations = list(REQUIRED_TABLES + REQUIRED_VIEWS)
    cursor.execute(
        """SELECT n.nspname || '.' || c.relname, c.relkind
             FROM pg_class c
             JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE (n.nspname || '.' || c.relname) = ANY(%s)""",
        (expected_relations,),
    )
    relation_kinds = {name: kind for name, kind in cursor.fetchall()}
    tables = {
        name: relation_kinds.get(name) in {"r", "p"}
        for name in REQUIRED_TABLES
    }
    views = {name: relation_kinds.get(name) == "v" for name in REQUIRED_VIEWS}

    column_pairs = list(REQUIRED_COLUMNS)
    cursor.execute(
        """SELECT table_schema || '.' || table_name, column_name
             FROM information_schema.columns
            WHERE (table_schema || '.' || table_name) = ANY(%s)
              AND column_name = ANY(%s)""",
        ([relation for relation, _ in column_pairs],
         [column for _, column in column_pairs]),
    )
    present_columns = {(relation, column) for relation, column in cursor.fetchall()}
    columns = {
        f"{relation}.{column}": (relation, column) in present_columns
        for relation, column in column_pairs
    }

    index_keys = [f"{relation}.{name}" for relation, name, _ in REQUIRED_INDEXES]
    cursor.execute(
        """SELECT tn.nspname || '.' || tc.relname || '.' || ic.relname,
                          i.indisunique,
                          ARRAY(
                              SELECT a.attname
                                FROM unnest(i.indkey) WITH ORDINALITY AS idx_column(attnum, position)
                                JOIN pg_attribute a
                                  ON a.attrelid=tc.oid AND a.attnum=idx_column.attnum
                               ORDER BY idx_column.position
                          )
             FROM pg_index i
             JOIN pg_class ic ON ic.oid=i.indexrelid
             JOIN pg_class tc ON tc.oid=i.indrelid
             JOIN pg_namespace tn ON tn.oid=tc.relnamespace
            WHERE (tn.nspname || '.' || tc.relname || '.' || ic.relname) = ANY(%s)""",
        (index_keys,),
    )
    present_indexes = {
        name: (bool(unique), tuple(columns or ()))
        for name, unique, columns in cursor.fetchall()
    }
    indexes = {
        f"{relation}.{name}": present_indexes.get(f"{relation}.{name}") == (True, tuple(columns))
        for relation, name, columns in REQUIRED_INDEXES
    }

    cursor.execute(
        """SELECT required.signature,
                          to_regprocedure(required.signature) IS NOT NULL
             FROM unnest(%s::text[]) AS required(signature)""",
        (list(REQUIRED_FUNCTIONS),),
    )
    functions = {name: bool(found) for name, found in cursor.fetchall()}

    cursor.execute(
        """SELECT n.nspname || '.' || c.relname || '.' || t.tgname
             FROM pg_trigger t
             JOIN pg_class c ON c.oid=t.tgrelid
             JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE NOT t.tgisinternal
              AND (n.nspname || '.' || c.relname || '.' || t.tgname) = ANY(%s)""",
        (list(REQUIRED_TRIGGERS),),
    )
    present_triggers = {row[0] for row in cursor.fetchall()}
    triggers = {name: name in present_triggers for name in REQUIRED_TRIGGERS}

    return {
        "schemas": {name: name in schemas for name in REQUIRED_SCHEMAS},
        "tables": tables,
        "views": views,
        "columns": columns,
        "indexes": indexes,
        "functions": functions,
        "triggers": triggers,
        "ok": (
            all(schemas.values()) and all(tables.values()) and all(views.values())
            and all(columns.values()) and all(indexes.values())
            and all(functions.values()) and all(triggers.values())
        ),
    }
