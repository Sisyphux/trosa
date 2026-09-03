#!/usr/bin/env python3
"""Repeatable, non-production importer for the unified Trade OS PostgreSQL DB.

Sources are opened read-only. Every source row is archived in
audit.legacy_records before the business projections below are written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

try:
    from tools.postgres_source_inventory import discover_trosa_db_names
except ModuleNotFoundError:  # direct ``python tools/...`` invocation
    from postgres_source_inventory import discover_trosa_db_names

ORG_ID = uuid.uuid5(uuid.NAMESPACE_URL, "trade-os:organization")
NS = uuid.uuid5(uuid.NAMESPACE_URL, "trade-os:unified-import")


def uid(value: str) -> uuid.UUID:
    return uuid.uuid5(NS, value)


def clean(value: Any) -> str:
    return str(value or "").strip()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean(value).lower())).strip()


def domain(value: Any) -> str:
    value = clean(value).lower()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.netloc.split("@")[-1].split(":")[0].removeprefix("www.")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def directory_sha256(path: Path) -> str:
    """Hash a deterministic directory snapshot without modifying it."""
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        digest.update(str(child.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        with child.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def parse_time(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            text += "T00:00:00+08:00"
        elif "T" not in text and re.match(r"\d{4}-\d{2}-\d{2} ", text):
            text = text.replace(" ", "T", 1) + "+08:00"
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = clean(value)
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return value


def sqlite_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        names = [row[0] for row in con.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )]
        return {name: [dict(row) for row in con.execute(f'select * from "{name}"')] for name in names}
    finally:
        con.close()


class Importer:
    def __init__(self, dsn: str, trosa_dir: Path, sela_dir: Path, sela_runs_dir: Path | None = None):
        self.dsn, self.trosa_dir, self.sela_dir = dsn, trosa_dir, sela_dir
        self.sela_runs_dir = sela_runs_dir
        self.report: dict[str, Any] = {"organization_id": str(ORG_ID), "sources": {}, "target": {}, "issues": []}
        self.accounts: dict[str, uuid.UUID] = {}
        self.prospects: dict[str, uuid.UUID] = {}

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.cur.execute(sql, params)

    @staticmethod
    def legacy_id(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def account_ref(self, db_name: str, customer_id: Any, account_id: uuid.UUID) -> None:
        legacy_customer_id = self.legacy_id(customer_id)
        if legacy_customer_id is None:
            return
        legacy_user = Path(db_name).stem
        self.execute("""insert into trosa.account_legacy_refs
          (organization_id,legacy_user_id,legacy_customer_id,account_id,source_db)
          values (%s,%s,%s,%s,%s)
          on conflict (organization_id,legacy_user_id,legacy_customer_id)
          do update set account_id=excluded.account_id,source_db=excluded.source_db""",
          (ORG_ID, legacy_user, legacy_customer_id, account_id, db_name))

    def row_ref(self, db_name: str, table_name: str, legacy_id: Any, target_id: uuid.UUID) -> None:
        number = self.legacy_id(legacy_id)
        if number is None:
            return
        self.execute("""insert into trosa.legacy_row_refs
          (organization_id,legacy_user_id,table_name,legacy_id,target_id)
          values (%s,%s,%s,%s,%s)
          on conflict (organization_id,legacy_user_id,table_name,legacy_id)
          do update set target_id=excluded.target_id""",
          (ORG_ID, Path(db_name).stem, table_name, number, target_id))

    def account_for(self, db_name: str, customer_id: Any) -> uuid.UUID | None:
        return self.accounts.get(f"{db_name}:customer:{customer_id}")

    def ref_target(self, db_name: str, table_name: str, legacy_id: Any) -> uuid.UUID | None:
        number = self.legacy_id(legacy_id)
        if number is None:
            return None
        row = self.cur.execute(
            """select target_id from trosa.legacy_row_refs
               where organization_id=%s and legacy_user_id=%s and table_name=%s and legacy_id=%s""",
            (ORG_ID, Path(db_name).stem, table_name, number),
        ).fetchone()
        return row[0] if row else None

    def person_method_for_email(self, value: Any) -> uuid.UUID | None:
        email_value = clean(value).lower()
        if not email_value:
            return None
        row = self.cur.execute(
            "select id from core.contact_methods where organization_id=%s and kind='email' and normalized_value=%s",
            (ORG_ID, email_value),
        ).fetchone()
        return row[0] if row else None

    def issue(self, source: str, key: str, code: str, detail: str, payload: Any = None) -> None:
        issue_id = uid(f"issue:{source}:{key}:{code}")
        self.execute("""insert into audit.migration_issues
          (id,batch_id,severity,issue_code,source_table,legacy_key,detail,payload)
          values (%s,%s,'warning',%s,%s,%s,%s,%s)
          on conflict (id) do update set detail=excluded.detail,payload=excluded.payload""",
          (issue_id, self.batch_id, code, source, key, detail, Jsonb(payload or {})))
        self.report["issues"].append({"source": source, "key": key, "code": code, "detail": detail})

    def ensure_org(self) -> None:
        self.execute("""insert into identity.organizations(id,name) values (%s,'Trade OS')
          on conflict (id) do update set updated_at=now()""", (ORG_ID,))

    def archive(self, source: str, key: str, row: dict[str, Any], row_number: int | None = None) -> None:
        payload = {name: json_value(value) for name, value in row.items()}
        record_id = uid(f"raw:{self.batch_id}:{source}:{key}")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        self.execute("""insert into audit.legacy_records
          (id,batch_id,source_table,legacy_key,legacy_row_number,payload,payload_sha256)
          values (%s,%s,%s,%s,%s,%s,%s)
          on conflict (batch_id,source_table,legacy_key) do update set payload=excluded.payload,payload_sha256=excluded.payload_sha256""",
          (record_id, self.batch_id, source, key, row_number, Jsonb(payload), hashlib.sha256(encoded).hexdigest()))

    def company(self, name: Any, website: Any, country: Any, city: Any = "", business_type: Any = "") -> uuid.UUID:
        canonical, normalized, dom = clean(name), norm(name), domain(website)
        identity = f"domain:{dom}" if dom else f"name:{normalized}|{norm(country)}"
        company_id = uid(f"company:{identity}")
        if not canonical:
            canonical, normalized = "UNKNOWN", "unknown"
            self.issue("core.companies", identity, "MISSING_COMPANY_NAME", "Preserved only as an unresolved identity")
        self.execute("""insert into core.companies
          (id,organization_id,canonical_name,normalized_name,website,country_code,city,business_type)
          values (%s,%s,%s,%s,%s,%s,%s,%s)
          on conflict (id) do update set updated_at=now()""",
          (company_id, ORG_ID, canonical, normalized, clean(website), clean(country), clean(city), clean(business_type)))
        if dom:
            domain_id = uid(f"company-domain:{dom}")
            self.execute("""insert into core.company_domains(id,company_id,normalized_domain,source_url,is_primary,verification_status)
              values (%s,%s,%s,%s,true,'imported') on conflict (company_id,normalized_domain) do nothing""",
              (domain_id, company_id, dom, clean(website)))
        return company_id

    def email(self, company_id: uuid.UUID, value: Any, person_id: uuid.UUID | None = None, evidence: Any = None) -> uuid.UUID | None:
        value = clean(value).lower()
        if not value or "@" not in value:
            return None
        method_id = uid(f"email:{value}")
        self.execute("""insert into core.contact_methods
          (id,organization_id,company_id,person_id,kind,value,normalized_value,evidence)
          values (%s,%s,%s,%s,'email',%s,%s,%s)
          on conflict (organization_id,kind,normalized_value) do update set updated_at=now()""",
          (method_id, ORG_ID, None if person_id else company_id, person_id, value, value, Jsonb(evidence or {})))
        return method_id

    def import_trosa(self) -> None:
        db_names, discovery_errors = discover_trosa_db_names(self.trosa_dir)
        if discovery_errors:
            raise ValueError("Trosa source set is not closed: " + "; ".join(discovery_errors))
        for db_name in db_names:
            path = self.trosa_dir / db_name
            rows = sqlite_rows(path)
            source_hash = sha256(path)
            source_name = f"trosa/{db_name}"
            self.register_batch(source_name, str(path), source_hash, sum(map(len, rows.values())))
            for table, values in rows.items():
                for number, row in enumerate(values, 1):
                    self.archive(f"{source_name}/{table}", clean(row.get("id") or number), row, number)
            if db_name == "system.db":
                for row in rows.get("users", []):
                    user_id = uid(f"user:{clean(row['id'])}")
                    legacy_id = clean(row["id"])
                    role = clean(row.get("role")) or ('admin' if legacy_id == 'hamid' else 'member')
                    self.execute("""insert into identity.users
                      (id,organization_id,legacy_user_id,username,display_name,label,color,password_hash,role,created_by,active,legacy_payload)
                      values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      on conflict (organization_id,legacy_user_id) do update set
                        username=excluded.username,display_name=excluded.display_name,label=excluded.label,color=excluded.color,
                        password_hash=excluded.password_hash,role=excluded.role,created_by=excluded.created_by,active=excluded.active,
                        legacy_payload=excluded.legacy_payload,updated_at=now()""",
                      (user_id, ORG_ID, legacy_id, clean(row.get("username")) or legacy_id,
                       clean(row.get("name")), clean(row.get("label")), clean(row.get("color")),
                       clean(row.get("password_hash")), role, clean(row.get("created_by")),
                       bool(row.get("active", 1)), Jsonb(row)))
                    self.execute("insert into identity.memberships(organization_id,user_id,role) values (%s,%s,%s) on conflict (organization_id,user_id) do update set role=excluded.role", (ORG_ID, user_id, role))
                continue
            for row in rows.get("customers", []):
                key = f"{db_name}:customer:{row['id']}"
                company_id = self.company(row.get("company") or row.get("name"), row.get("website"), row.get("country"), business_type=row.get("type"))
                account_id = uid(f"account:{company_id}")
                self.accounts[key] = account_id
                self.execute("""insert into trosa.accounts(id,organization_id,company_id,owner_user_id,display_name,account_status,customer_type,channel_type,priority_level,profile,field,industry,company_size,annual_revenue,tags,attention_state,attention_reason,last_contact_at,next_follow_up_at,deleted_at,legacy_payload)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (organization_id,company_id) do nothing""",
                  (account_id, ORG_ID, company_id, uid(f"user:{Path(db_name).stem}"), clean(row.get("name")), clean(row.get("status")), clean(row.get("customer_type")), clean(row.get("type")), clean(row.get("level")), clean(row.get("profile")), clean(row.get("field")), clean(row.get("industry")), clean(row.get("company_size")), clean(row.get("annual_revenue")), clean(row.get("tags")), clean(row.get("attention_state")), clean(row.get("attention_reason")), parse_time(row.get("last_contact")), parse_time(row.get("next_follow_up")), parse_time(row.get("deleted_at")) if clean(row.get("is_deleted")) == "1" else None, Jsonb(row)))
                self.account_ref(db_name, row.get("id"), account_id)
            for row in rows.get("contacts", []):
                account_id = self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}")
                if not account_id: continue
                company_id = self.cur.execute("select company_id from trosa.accounts where id=%s", (account_id,)).fetchone()[0]
                email_value = clean(row.get("email")).lower()
                person_id = uid(f"person:email:{email_value}") if email_value else uid(f"person:{db_name}:{row['id']}")
                self.execute("insert into core.people(id,organization_id,full_name,normalized_name) values (%s,%s,%s,%s) on conflict (id) do update set full_name=case when core.people.full_name='' or core.people.full_name='UNKNOWN' then excluded.full_name else core.people.full_name end,updated_at=now()", (person_id, ORG_ID, clean(row.get("name")) or "UNKNOWN", norm(row.get("name"))))
                self.execute("insert into core.company_people(id,company_id,person_id,title,source) values (%s,%s,%s,%s,'trosa') on conflict do nothing", (uid(f"company-person:{company_id}:{person_id}:{clean(row.get('title'))}"), company_id, person_id, clean(row.get("title"))))
                contact_method_id = self.email(company_id, email_value, person_id, {"source": "trosa.contacts", "legacy_id": row["id"]})
                legacy_contact_id = self.legacy_id(row.get("id"))
                legacy_customer_id = self.legacy_id(row.get("customer_id"))
                if legacy_contact_id is not None and legacy_customer_id is not None:
                    self.execute("""insert into trosa.contact_legacy_refs
                      (organization_id,legacy_user_id,legacy_contact_id,legacy_customer_id,account_id,person_id,contact_method_id,name,title,phone,whatsapp,linkedin,preferred_channel,contact_type,is_primary,notes,legacy_payload)
                      values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      on conflict (organization_id,legacy_user_id,legacy_contact_id) do update set
                      legacy_customer_id=excluded.legacy_customer_id,account_id=excluded.account_id,
                      person_id=excluded.person_id,contact_method_id=excluded.contact_method_id,
                      name=excluded.name,title=excluded.title,phone=excluded.phone,whatsapp=excluded.whatsapp,
                      linkedin=excluded.linkedin,preferred_channel=excluded.preferred_channel,
                      contact_type=excluded.contact_type,is_primary=excluded.is_primary,notes=excluded.notes,
                      legacy_payload=excluded.legacy_payload,updated_at=now()""",
                      (ORG_ID, Path(db_name).stem, legacy_contact_id, legacy_customer_id, account_id,
                       person_id, contact_method_id, clean(row.get("name")), clean(row.get("title")),
                       clean(row.get("phone")), clean(row.get("whatsapp")), clean(row.get("linkedin")),
                       clean(row.get("preferred_channel")), clean(row.get("contact_type")) or "person",
                       bool(row.get("is_primary")), clean(row.get("notes")), Jsonb(row)))
            for row in rows.get("reminders", []):
                account_id=self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}"); due=parse_time(row.get("remind_date"))
                if account_id and due:
                    target_id = uid(f"task:{db_name}:{row['id']}")
                    self.execute("""insert into trosa.tasks(id,account_id,title,content,reason,due_at,status,task_type,source_activity_legacy_id,manual_order,completed_at,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set source_activity_legacy_id=excluded.source_activity_legacy_id,legacy_payload=excluded.legacy_payload""", (target_id,account_id,clean(row.get("title")),clean(row.get("content")),clean(row.get("reason")),due,'done' if row.get("is_done") else 'open',clean(row.get("reminder_type")),clean(row.get("source_activity_id")),int(row.get("manual_order") or 0),parse_time(row.get("completed_at")),Jsonb(row)))
                    self.row_ref(db_name, "reminders", row.get("id"), target_id)
            for row in rows.get("follow_up_logs", []):
                account_id=self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}"); occurred=parse_time(row.get("follow_date") or row.get("created_at"))
                if account_id and occurred:
                    target_id = uid(f"timeline:{db_name}:{row['id']}")
                    self.execute("""insert into trosa.timeline_events(id,account_id,event_type,direction,content,result,next_plan,source_module,source_reference,occurred_at,payload) values (%s,%s,%s,%s,%s,%s,%s,'trosa',%s,%s,%s) on conflict (id) do update set payload=excluded.payload""", (target_id,account_id,clean(row.get("activity_type")),clean(row.get("direction")),clean(row.get("content")),clean(row.get("result")),clean(row.get("next_plan")),f"{db_name}:{row['id']}",occurred,Jsonb(row)))
                    self.row_ref(db_name, "follow_up_logs", row.get("id"), target_id)
            for row in rows.get("outreach_emails", []):
                account_id=self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}")
                if account_id:
                    target_id = uid(f"trosa-message:{db_name}:{row['id']}")
                    self.execute("""insert into trosa.outreach_messages(id,account_id,subject,body,sent_at,reply_status,reply_content,reply_at,provider,provider_message_id,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set legacy_payload=excluded.legacy_payload""", (target_id,account_id,clean(row.get("subject")),clean(row.get("content")),parse_time(row.get("sent_date")),clean(row.get("reply_status")),clean(row.get("reply_content")),parse_time(row.get("reply_date")),"legacy",clean(row.get("external_id")),Jsonb(row)))
                    self.row_ref(db_name, "outreach_emails", row.get("id"), target_id)

            self.import_trosa_secondary(db_name, rows)

    def import_trosa_secondary(self, db_name: str, rows: dict[str, list[dict[str, Any]]]) -> None:
        """Project the remaining Trosa tables without recreating core entities."""
        user = Path(db_name).stem

        def account_id(row: dict[str, Any]) -> uuid.UUID | None:
            return self.account_for(db_name, row.get("customer_id"))

        # Research and customer AI state use the normalized module tables.
        for row in rows.get("research_reports", []):
            account = account_id(row)
            if not account:
                continue
            target = self.ref_target(db_name, "research_reports", row.get("id"))
            if not target:
                target = self.cur.execute("select id from trosa.research_reports where account_id=%s", (account,)).fetchone()
                target = target[0] if target else uid(f"research:{db_name}:{row.get('id')}")
            self.execute("""insert into trosa.research_reports
              (id,account_id,summary,company_info,key_findings,needs_analysis,cooperation_value,raw_input,
               source,web_content,web_fetched_at,expires_at,legacy_payload,created_at,updated_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (account_id) do update set summary=excluded.summary,company_info=excluded.company_info,
               key_findings=excluded.key_findings,needs_analysis=excluded.needs_analysis,
               cooperation_value=excluded.cooperation_value,raw_input=excluded.raw_input,source=excluded.source,
               web_content=excluded.web_content,web_fetched_at=excluded.web_fetched_at,expires_at=excluded.expires_at,
               legacy_payload=trosa.research_reports.legacy_payload||excluded.legacy_payload,updated_at=excluded.updated_at""",
                (target, account, clean(row.get("summary")), clean(row.get("company_info")), clean(row.get("key_findings")),
                 clean(row.get("needs_analysis")), clean(row.get("cooperation_value")), clean(row.get("raw_input")),
                 clean(row.get("source")), clean(row.get("web_content")), clean(row.get("web_fetched_at")),
                 clean(row.get("expires_at")), Jsonb(row), parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                 parse_time(row.get("updated_at")) or datetime.now(timezone.utc)))
            actual = self.cur.execute("select id from trosa.research_reports where account_id=%s", (account,)).fetchone()[0]
            self.row_ref(db_name, "research_reports", row.get("id"), actual)

        for row in rows.get("external_analysis_notes", []):
            account = account_id(row)
            if not account:
                continue
            target = uid(f"external-note:{db_name}:{row.get('id')}")
            self.execute("""insert into trosa.external_analysis_notes
              (id,account_id,content,source,legacy_payload,created_at,updated_at)
              values (%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set content=excluded.content,
              source=excluded.source,legacy_payload=excluded.legacy_payload,updated_at=excluded.updated_at""",
                (target, account, clean(row.get("content")), clean(row.get("source")) or "external_model",
                 Jsonb(row), parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                 parse_time(row.get("updated_at")) or datetime.now(timezone.utc)))
            self.row_ref(db_name, "external_analysis_notes", row.get("id"), target)

        for row in rows.get("customer_understandings", []):
            account = account_id(row)
            if not account:
                continue
            target = self.ref_target(db_name, "customer_understandings", row.get("id"))
            if not target:
                existing = self.cur.execute("select id from trosa.account_understandings where account_id=%s", (account,)).fetchone()
                target = existing[0] if existing else uid(f"understanding:{account}")
            source_event = self.ref_target(db_name, "follow_up_logs", row.get("source_activity_id"))
            self.execute("""insert into trosa.account_understandings
              (id,account_id,current_summary,recent_change,open_loops,action_state,action_reason,
               source_timeline_event_id,version,created_at,updated_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (account_id) do update set current_summary=excluded.current_summary,
               recent_change=excluded.recent_change,open_loops=excluded.open_loops,action_state=excluded.action_state,
               action_reason=excluded.action_reason,source_timeline_event_id=excluded.source_timeline_event_id,
               version=excluded.version,updated_at=excluded.updated_at""",
                (target, account, clean(row.get("current_summary")), clean(row.get("recent_change")),
                 Jsonb(json_value(row.get("open_loops")) if isinstance(json_value(row.get("open_loops")), list) else []),
                 clean(row.get("action_state")) or "hold", clean(row.get("action_reason")), source_event,
                 int(row.get("version") or 1), parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                 parse_time(row.get("updated_at")) or datetime.now(timezone.utc)))
            actual = self.cur.execute("select id from trosa.account_understandings where account_id=%s", (account,)).fetchone()[0]
            self.row_ref(db_name, "customer_understandings", row.get("id"), actual)

        for row in rows.get("ai_recommendations", []):
            account = account_id(row)
            if not account:
                continue
            target = uid(f"recommendation:{db_name}:{row.get('id')}")
            self.execute("""insert into trosa.ai_recommendations
              (id,account_id,understanding_version,content,reason,source_timeline_event_id,review_status,
               user_response,user_modified_content,executed_action,outcome,created_at,updated_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (id) do update set content=excluded.content,reason=excluded.reason,
               source_timeline_event_id=excluded.source_timeline_event_id,review_status=excluded.review_status,
               user_response=excluded.user_response,user_modified_content=excluded.user_modified_content,
               executed_action=excluded.executed_action,outcome=excluded.outcome,updated_at=excluded.updated_at""",
                (target, account, int(row.get("understanding_version") or 0), clean(row.get("content")), clean(row.get("reason")),
                 self.ref_target(db_name, "follow_up_logs", row.get("source_activity_id")), clean(row.get("review_status")) or "hold",
                 clean(row.get("user_response")), clean(row.get("user_modified_content")), clean(row.get("executed_action")),
                 clean(row.get("outcome")), parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                 parse_time(row.get("updated_at")) or datetime.now(timezone.utc)))
            self.row_ref(db_name, "ai_recommendations", row.get("id"), target)

        for row in rows.get("inbox_items", []):
            account = account_id(row)
            target = uid(f"inbox:{db_name}:{row.get('id')}")
            self.execute("""insert into trosa.inbox_items
              (id,account_id,item_type,title,content,dedupe_key,status,snoozed_until,resolved_at,
               resolution_reason,resolution_note,legacy_payload,created_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (id) do update set account_id=excluded.account_id,item_type=excluded.item_type,
               title=excluded.title,content=excluded.content,status=excluded.status,snoozed_until=excluded.snoozed_until,
               resolved_at=excluded.resolved_at,resolution_reason=excluded.resolution_reason,
               resolution_note=excluded.resolution_note,legacy_payload=excluded.legacy_payload""",
                (target, account, clean(row.get("item_type")), clean(row.get("title")), clean(row.get("content")),
                 clean(row.get("dedupe_key")), clean(row.get("status")) or "open", parse_time(row.get("snoozed_until")),
                 parse_time(row.get("resolved_at")), clean(row.get("resolution_reason")), clean(row.get("resolution_note")),
                 Jsonb(row), parse_time(row.get("created_at")) or datetime.now(timezone.utc)))
            self.row_ref(db_name, "inbox_items", row.get("id"), target)

        for row in rows.get("web_monitor_logs", []):
            account = account_id(row)
            if not account:
                continue
            target = uid(f"web-monitor:{db_name}:{row.get('id')}")
            self.execute("""insert into trosa.web_monitor_observations
              (id,account_id,url,status,content_hash,content_snippet,change_summary,checked_at,task_id,legacy_payload)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set url=excluded.url,status=excluded.status,
              content_hash=excluded.content_hash,content_snippet=excluded.content_snippet,change_summary=excluded.change_summary,
              checked_at=excluded.checked_at,task_id=excluded.task_id,legacy_payload=excluded.legacy_payload""",
                (target, account, clean(row.get("url")), clean(row.get("status")) or "ok", clean(row.get("content_hash")),
                 clean(row.get("content_snippet")), clean(row.get("change_summary")),
                 parse_time(row.get("checked_at")) or datetime.now(timezone.utc),
                 self.ref_target(db_name, "reminders", row.get("reminder_id")), Jsonb(row)))
            self.row_ref(db_name, "web_monitor_logs", row.get("id"), target)

        # File binaries remain filesystem objects; their metadata and one
        # unified entity relation are written to PostgreSQL.
        for row in rows.get("customer_files", []):
            account = account_id(row)
            if not account:
                continue
            number = self.legacy_id(row.get("id"))
            if number is None:
                continue
            file_id = uid(f"file:{user}:{number}")
            self.execute("""insert into core.file_objects
              (id,organization_id,storage_key,original_name,mime_type,size_bytes,sha256,uploaded_by_user_id,deleted_at,created_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (id) do update set storage_key=excluded.storage_key,original_name=excluded.original_name,
              mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,sha256=excluded.sha256,deleted_at=excluded.deleted_at""",
                (file_id, ORG_ID, clean(row.get("file_path")), clean(row.get("original_name")), clean(row.get("mime_type")),
                 int(row.get("file_size") or 0), clean(row.get("sha256")), uid(f"user:{user}"),
                 parse_time(row.get("deleted_at")) if clean(row.get("is_deleted")) == "1" else None,
                 parse_time(row.get("created_at")) or datetime.now(timezone.utc)))
            self.execute("""insert into core.entity_files(id,file_object_id,account_id,relation_type)
              values (%s,%s,%s,'attachment') on conflict do nothing""", (uid(f"entity-file:{user}:{number}"), file_id, account))
            self.execute("""insert into trade_os_compat.customer_file_rows
              (legacy_user_id,id,customer_id,account_id,file_object_id,original_name,stored_name,file_path,file_size,mime_type,category,sha256,uploaded_by,is_deleted,deleted_at,created_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (legacy_user_id,id) do update set customer_id=excluded.customer_id,account_id=excluded.account_id,
              file_object_id=excluded.file_object_id,original_name=excluded.original_name,stored_name=excluded.stored_name,
              file_path=excluded.file_path,file_size=excluded.file_size,mime_type=excluded.mime_type,category=excluded.category,
              sha256=excluded.sha256,uploaded_by=excluded.uploaded_by,is_deleted=excluded.is_deleted,deleted_at=excluded.deleted_at""",
                (user, number, self.legacy_id(row.get("customer_id")), account, file_id, clean(row.get("original_name")),
                 clean(row.get("stored_name")), clean(row.get("file_path")), int(row.get("file_size") or 0), clean(row.get("mime_type")),
                 clean(row.get("category")), clean(row.get("sha256")), clean(row.get("uploaded_by")), int(row.get("is_deleted") or 0),
                 clean(row.get("deleted_at")), clean(row.get("created_at"))))

        self.import_trosa_runtime_rows(db_name, rows)

    def import_trosa_runtime_rows(self, db_name: str, rows: dict[str, list[dict[str, Any]]]) -> None:
        """Preserve user-scoped audit/integration ledgers in PostgreSQL runtime tables."""
        user = Path(db_name).stem

        def next_id(table: str) -> int:
            if table.startswith('email_'):
                source_table = {
                    'email_verifications': 'email_verifications',
                    'email_verification_jobs': 'email_verification_jobs',
                    'email_domain_probes': 'email_domain_probes',
                    'email_logs': 'email_logs',
                }[table]
                row = self.cur.execute(
                    f"select coalesce(max(id),0)+1 from trosa.{source_table} where organization_id=%s and legacy_user_id=%s",
                    (ORG_ID, user),
                ).fetchone()
            else:
                row = self.cur.execute(f"select coalesce(max(id),0)+1 from trade_os_compat.{table} where legacy_user_id=%s", (user,)).fetchone()
            return int(row[0])

        for row in rows.get("operation_logs", []):
            number = self.legacy_id(row.get("id")) or next_id("operation_log_rows")
            self.execute("""insert into trade_os_compat.operation_log_rows(legacy_user_id,id,action,target_type,target_id,details,created_at,user_id)
              values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (legacy_user_id,id) do update set action=excluded.action,target_type=excluded.target_type,target_id=excluded.target_id,details=excluded.details,created_at=excluded.created_at,user_id=excluded.user_id""",
              (user,number,clean(row.get("action")),clean(row.get("target_type")),self.legacy_id(row.get("target_id")),clean(row.get("details")),clean(row.get("created_at")),clean(row.get("user_id")) or user))
            actor = self.cur.execute("select id from identity.users where organization_id=%s and legacy_user_id=%s", (ORG_ID,user)).fetchone()
            self.execute("""insert into audit.events(id,organization_id,actor_user_id,actor_type,action,entity_type,entity_id,after_payload,occurred_at)
              values (%s,%s,%s,'user',%s,%s,NULL,%s,%s) on conflict(id) do update set after_payload=excluded.after_payload,occurred_at=excluded.occurred_at""",
              (uid(f"operation:{db_name}:{number}"),ORG_ID,actor[0] if actor else None,clean(row.get("action")),clean(row.get("target_type")),Jsonb({"legacy_id":number,"details":clean(row.get("details"))}),parse_time(row.get("created_at")) or datetime.now(timezone.utc)))

        for row in rows.get("agent_proposals", []):
            number=self.legacy_id(row.get("id")) or next_id("agent_proposal_rows")
            self.execute("""insert into trade_os_compat.agent_proposal_rows(legacy_user_id,id,proposal_type,customer_id,payload,proposal_action,source,source_reference,idempotency_key,request_sha256,status,created_at,confirmed_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set payload=excluded.payload,status=excluded.status,confirmed_at=excluded.confirmed_at""",
              (user,number,clean(row.get("proposal_type")),self.legacy_id(row.get("customer_id")) or 0,clean(row.get("payload")) or "{}",clean(row.get("proposal_action")),clean(row.get("source")),clean(row.get("source_reference")),clean(row.get("idempotency_key")),clean(row.get("request_sha256")),clean(row.get("status")) or "pending",clean(row.get("created_at")),clean(row.get("confirmed_at"))))
            account=self.account_for(db_name,row.get("customer_id"))
            self.execute("""insert into audit.agent_proposals(id,organization_id,account_id,proposal_type,payload,proposal_action,source,source_reference,idempotency_key,request_sha256,status,created_at,confirmed_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(id) do update set payload=excluded.payload,status=excluded.status,confirmed_at=excluded.confirmed_at""",
              (uid(f"proposal:{db_name}:{number}"),ORG_ID,account,clean(row.get("proposal_type")),Jsonb(json_value(row.get("payload")) or {}),clean(row.get("proposal_action")),clean(row.get("source")),clean(row.get("source_reference")),clean(row.get("idempotency_key")),clean(row.get("request_sha256")),clean(row.get("status")) or "pending",parse_time(row.get("created_at")) or datetime.now(timezone.utc),parse_time(row.get("confirmed_at"))))

        for row in rows.get("agent_actions", []):
            number=self.legacy_id(row.get("id")) or next_id("agent_action_rows")
            self.execute("""insert into trade_os_compat.agent_action_rows(legacy_user_id,id,action_id,token_id,user_id,action_type,customer_id,related_type,related_id,undo_token,request_json,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set status=excluded.status,undone_at=excluded.undone_at""",
              (user,number,clean(row.get("action_id")),clean(row.get("token_id")),clean(row.get("user_id")) or user,clean(row.get("action_type")),self.legacy_id(row.get("customer_id")),clean(row.get("related_type")),self.legacy_id(row.get("related_id")),clean(row.get("undo_token")),clean(row.get("request_json")) or "{}",clean(row.get("status")) or "completed",clean(row.get("created_at")),clean(row.get("undone_at"))))
            account=self.account_for(db_name,row.get("customer_id"))
            actor=self.cur.execute("select id from identity.users where organization_id=%s and legacy_user_id=%s",(ORG_ID,user)).fetchone()
            self.execute("""insert into audit.agent_actions(id,organization_id,action_id,token_id,actor_user_id,action_type,account_id,related_type,related_id,undo_token,request_payload,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(id) do update set status=excluded.status,undone_at=excluded.undone_at""",
              (uid(f"agent-action:{db_name}:{number}"),ORG_ID,clean(row.get("action_id")),clean(row.get("token_id")),actor[0] if actor else None,clean(row.get("action_type")),account,clean(row.get("related_type")),clean(row.get("related_id")),clean(row.get("undo_token")),Jsonb(json_value(row.get("request_json")) or {}),clean(row.get("status")) or "completed",parse_time(row.get("created_at")) or datetime.now(timezone.utc),parse_time(row.get("undone_at"))))

        for row in rows.get("undo_actions", []):
            number=self.legacy_id(row.get("id")) or next_id("undo_action_rows")
            entities=json_value(row.get("entities")) or []
            self.execute("""insert into trade_os_compat.undo_action_rows(legacy_user_id,id,token,operation,target_type,target_id,description,entities,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set entities=excluded.entities,status=excluded.status,undone_at=excluded.undone_at""",
              (user,number,clean(row.get("token")),clean(row.get("operation")),clean(row.get("target_type")),self.legacy_id(row.get("target_id")),clean(row.get("description")),json.dumps(entities,ensure_ascii=False),clean(row.get("status")) or "available",clean(row.get("created_at")),clean(row.get("undone_at"))))
            self.execute("""insert into audit.undo_snapshots(id,organization_id,token,operation,target_type,target_id,description,entities,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(id) do update set entities=excluded.entities,status=excluded.status,undone_at=excluded.undone_at""",
              (uid(f"undo:{db_name}:{number}"),ORG_ID,clean(row.get("token")),clean(row.get("operation")),clean(row.get("target_type")),clean(row.get("target_id")),clean(row.get("description")),Jsonb(entities),clean(row.get("status")) or "available",parse_time(row.get("created_at")) or datetime.now(timezone.utc),parse_time(row.get("undone_at"))))

        for row in rows.get("email_verifications", []):
            number=self.legacy_id(row.get("id")) or next_id("email_verifications")
            self.execute("""insert into trosa.email_verifications(organization_id,legacy_user_id,legacy_id,email,normalized_email,domain,deliverability_status,confidence,address_type,risk_flags,evidence,mx_records,checked_at,expires_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,legacy_user_id,legacy_id) where legacy_id is not null do update set email=excluded.email,normalized_email=excluded.normalized_email,domain=excluded.domain,deliverability_status=excluded.deliverability_status,confidence=excluded.confidence,risk_flags=excluded.risk_flags,evidence=excluded.evidence,mx_records=excluded.mx_records,checked_at=excluded.checked_at,expires_at=excluded.expires_at""",
              (ORG_ID,user,number,clean(row.get("email")),clean(row.get("normalized_email")) or clean(row.get("email")),clean(row.get("domain")),clean(row.get("deliverability_status")) or "unknown",clean(row.get("confidence")) or "low",clean(row.get("address_type")) or "person",Jsonb(json_value(row.get("risk_flags")) or []),Jsonb(json_value(row.get("evidence")) or []),Jsonb(json_value(row.get("mx_records")) or []),clean(row.get("checked_at")),clean(row.get("expires_at"))))
        for row in rows.get("email_verification_jobs", []):
            number=self.legacy_id(row.get("id")) or next_id("email_verification_jobs")
            self.execute("""insert into trosa.email_verification_jobs(organization_id,legacy_user_id,legacy_id,email,domain,status,attempts,next_run_at,last_error,created_at,updated_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,legacy_user_id,legacy_id) where legacy_id is not null do update set email=excluded.email,domain=excluded.domain,status=excluded.status,attempts=excluded.attempts,next_run_at=excluded.next_run_at,last_error=excluded.last_error,updated_at=excluded.updated_at""",
              (ORG_ID,user,number,clean(row.get("email")),clean(row.get("domain")),clean(row.get("status")) or "queued",int(row.get("attempts") or 0),clean(row.get("next_run_at")),clean(row.get("last_error")),clean(row.get("created_at")),clean(row.get("updated_at"))))
        for row in rows.get("email_domain_probes", []):
            number=self.legacy_id(row.get("id")) or next_id("email_domain_probes")
            self.execute("""insert into trosa.email_domain_probes(organization_id,legacy_user_id,legacy_id,domain,catchall_status,evidence,checked_at,next_check_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,legacy_user_id,legacy_id) where legacy_id is not null do update set domain=excluded.domain,catchall_status=excluded.catchall_status,evidence=excluded.evidence,checked_at=excluded.checked_at,next_check_at=excluded.next_check_at""",
              (ORG_ID,user,number,clean(row.get("domain")),clean(row.get("catchall_status")) or "unknown",Jsonb(json_value(row.get("evidence")) or []),clean(row.get("checked_at")),clean(row.get("next_check_at"))))
        for row in rows.get("email_logs", []):
            number=self.legacy_id(row.get("id")) or next_id("email_logs")
            self.execute("""insert into trosa.email_logs(organization_id,legacy_user_id,legacy_key,status,message,reminder_count,created_at)
              values(%s,%s,%s,%s,%s,%s,%s) on conflict(organization_id,legacy_user_id,legacy_key) where legacy_key <> '' do update set status=excluded.status,message=excluded.message,reminder_count=excluded.reminder_count,created_at=excluded.created_at""",
              (ORG_ID,user,str(number),clean(row.get("status")),clean(row.get("message")),int(row.get("reminder_count") or 0),clean(row.get("created_at"))))

        for row in rows.get("import_batches", []):
            number=self.legacy_id(row.get("id")) or next_id("import_batch_rows")
            self.execute("""insert into trade_os_compat.import_batch_rows
              (legacy_user_id,id,source_name,source_sha256,imported_at,imported_count,skipped_count,created_customers,details)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              source_name=excluded.source_name,source_sha256=excluded.source_sha256,imported_at=excluded.imported_at,
              imported_count=excluded.imported_count,skipped_count=excluded.skipped_count,created_customers=excluded.created_customers,details=excluded.details""",
              (user,number,clean(row.get("source_name")),clean(row.get("source_sha256")),clean(row.get("imported_at")),int(row.get("imported_count") or 0),int(row.get("skipped_count") or 0),int(row.get("created_customers") or 0),clean(row.get("details"))))

        for row in rows.get("imported_activity_rows", []):
            number=self.legacy_id(row.get("id")) or next_id("imported_activity_row_rows")
            self.execute("""insert into trade_os_compat.imported_activity_row_rows
              (legacy_user_id,id,activity_hash,source_key,batch_id,customer_id,source_name,source_sheet,source_cell,source_header,activity_id,imported_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              activity_hash=excluded.activity_hash,source_key=excluded.source_key,batch_id=excluded.batch_id,customer_id=excluded.customer_id,
              source_name=excluded.source_name,source_sheet=excluded.source_sheet,source_cell=excluded.source_cell,source_header=excluded.source_header,
              activity_id=excluded.activity_id,imported_at=excluded.imported_at""",
              (user,number,clean(row.get("activity_hash")),clean(row.get("source_key")),self.legacy_id(row.get("batch_id")),self.legacy_id(row.get("customer_id")) or 0,clean(row.get("source_name")),clean(row.get("source_sheet")),clean(row.get("source_cell")),clean(row.get("source_header")),self.legacy_id(row.get("activity_id")),clean(row.get("imported_at"))))
            account=self.account_for(db_name,row.get("customer_id")); activity=self.ref_target(db_name,"follow_up_logs",row.get("activity_id"))
            if account:
                self.execute("""insert into audit.imported_activity_rows
                  (id,organization_id,legacy_user_id,activity_hash,source_key,batch_id,account_id,source_name,source_sheet,source_cell,source_header,activity_id)
                  values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict(organization_id,activity_hash) do update set source_key=excluded.source_key,account_id=excluded.account_id,activity_id=excluded.activity_id""",
                  (uid(f"imported-activity:{db_name}:{number}"),ORG_ID,user,clean(row.get("activity_hash")),clean(row.get("source_key")),self.batch_id,account,clean(row.get("source_name")),clean(row.get("source_sheet")),clean(row.get("source_cell")),clean(row.get("source_header")),activity))

        for row in rows.get("import_unmatched_customers", []):
            number=self.legacy_id(row.get("id")) or next_id("import_unmatched_customer_rows")
            self.execute("""insert into trade_os_compat.import_unmatched_customer_rows
              (legacy_user_id,id,unmatched_hash,batch_id,customer_name,country,website,source_sheet,source_row,reason,created_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              unmatched_hash=excluded.unmatched_hash,batch_id=excluded.batch_id,customer_name=excluded.customer_name,country=excluded.country,
              website=excluded.website,source_sheet=excluded.source_sheet,source_row=excluded.source_row,reason=excluded.reason,created_at=excluded.created_at""",
              (user,number,clean(row.get("unmatched_hash")),self.legacy_id(row.get("batch_id")),clean(row.get("customer_name")),clean(row.get("country")),clean(row.get("website")),clean(row.get("source_sheet")),self.legacy_id(row.get("source_row")),clean(row.get("reason")),clean(row.get("created_at"))))
            self.execute("""insert into audit.import_unmatched_customers
              (id,organization_id,legacy_user_id,unmatched_hash,batch_id,customer_name,country,website,source_sheet,source_row,reason)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,unmatched_hash) do update set customer_name=excluded.customer_name,reason=excluded.reason""",
              (uid(f"unmatched:{db_name}:{number}"),ORG_ID,user,clean(row.get("unmatched_hash")),self.batch_id,clean(row.get("customer_name")),clean(row.get("country")),clean(row.get("website")),clean(row.get("source_sheet")),self.legacy_id(row.get("source_row")),clean(row.get("reason"))))

        for row in rows.get("gmail_message_states", []):
            number=self.legacy_id(row.get("id")) or next_id("gmail_message_state_rows")
            self.execute("""insert into trade_os_compat.gmail_message_state_rows
              (legacy_user_id,id,provider_message_id,provider_thread_id,message_time,sender_email,recipient_emails,subject,customer_id,contact_id,match_status,activity_id,inbox_item_id,raw_payload,last_error,created_at,updated_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              provider_thread_id=excluded.provider_thread_id,message_time=excluded.message_time,sender_email=excluded.sender_email,
              recipient_emails=excluded.recipient_emails,subject=excluded.subject,customer_id=excluded.customer_id,contact_id=excluded.contact_id,
              match_status=excluded.match_status,activity_id=excluded.activity_id,inbox_item_id=excluded.inbox_item_id,raw_payload=excluded.raw_payload,last_error=excluded.last_error,updated_at=excluded.updated_at""",
              (user,number,clean(row.get("provider_message_id")),clean(row.get("provider_thread_id")),clean(row.get("message_time")),clean(row.get("sender_email")),clean(row.get("recipient_emails")) or "[]",clean(row.get("subject")),self.legacy_id(row.get("customer_id")),self.legacy_id(row.get("contact_id")),clean(row.get("match_status")) or "unmatched",self.legacy_id(row.get("activity_id")),self.legacy_id(row.get("inbox_item_id")),clean(row.get("raw_payload")) or "{}",clean(row.get("last_error")),clean(row.get("created_at")),clean(row.get("updated_at"))))

        for row in rows.get("communication_sources", []):
            number=self.legacy_id(row.get("id")) or next_id("communication_source_rows")
            self.execute("""insert into trade_os_compat.communication_source_rows
              (legacy_user_id,id,activity_id,channel,source_url,account,conversation_identity,adapter_version,extraction_scope,warnings,raw_payload,cleaned_payload,captured_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              activity_id=excluded.activity_id,channel=excluded.channel,source_url=excluded.source_url,account=excluded.account,
              conversation_identity=excluded.conversation_identity,adapter_version=excluded.adapter_version,extraction_scope=excluded.extraction_scope,
              warnings=excluded.warnings,raw_payload=excluded.raw_payload,cleaned_payload=excluded.cleaned_payload,captured_at=excluded.captured_at""",
              (user,number,self.legacy_id(row.get("activity_id")) or 0,clean(row.get("channel")),clean(row.get("source_url")),clean(row.get("account")),clean(row.get("conversation_identity")),clean(row.get("adapter_version")),clean(row.get("extraction_scope")),clean(row.get("warnings")) or "[]",clean(row.get("raw_payload")) or "{}",clean(row.get("cleaned_payload")),clean(row.get("captured_at"))))
            timeline=self.ref_target(db_name,"follow_up_logs",row.get("activity_id"))
            if timeline:
                source_id=uid(f"communication-source:{db_name}:{number}")
                self.execute("""insert into trosa.communication_sources(id,timeline_event_id,channel,source_url,account,conversation_identity,adapter_version,extraction_scope,warnings,raw_payload,cleaned_payload,captured_at)
                  values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(timeline_event_id) do update set raw_payload=excluded.raw_payload,cleaned_payload=excluded.cleaned_payload,captured_at=excluded.captured_at""",
                  (source_id,timeline,clean(row.get("channel")),clean(row.get("source_url")),clean(row.get("account")),clean(row.get("conversation_identity")),clean(row.get("adapter_version")),clean(row.get("extraction_scope")),Jsonb(json_value(row.get("warnings")) or []),Jsonb(json_value(row.get("raw_payload")) or {}),clean(row.get("cleaned_payload")),parse_time(row.get("captured_at"))))
                self.row_ref(db_name,"communication_sources",row.get("id"),source_id)

        for row in rows.get("communication_source_items", []):
            number=self.legacy_id(row.get("id")) or next_id("communication_source_item_rows")
            self.execute("""insert into trade_os_compat.communication_source_item_rows
              (legacy_user_id,id,source_fingerprint,activity_id,message_time,direction,raw_text)
              values(%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set source_fingerprint=excluded.source_fingerprint,activity_id=excluded.activity_id,message_time=excluded.message_time,direction=excluded.direction,raw_text=excluded.raw_text""",
              (user,number,clean(row.get("source_fingerprint")),self.legacy_id(row.get("activity_id")) or 0,clean(row.get("message_time")),clean(row.get("direction")) or "unknown",clean(row.get("raw_text"))))

        for row in rows.get("email_delivery_events", []):
            number=self.legacy_id(row.get("id")) or next_id("email_delivery_event_rows")
            self.execute("""insert into trade_os_compat.email_delivery_event_rows
              (legacy_user_id,id,email,contact_id,outreach_email_id,event_type,smtp_code,enhanced_status,diagnostic_text,remote_mta,message_id,source,occurred_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set email=excluded.email,event_type=excluded.event_type,diagnostic_text=excluded.diagnostic_text,occurred_at=excluded.occurred_at""",
              (user,number,clean(row.get("email")),self.legacy_id(row.get("contact_id")),self.legacy_id(row.get("outreach_email_id")),clean(row.get("event_type")),clean(row.get("smtp_code")),clean(row.get("enhanced_status")),clean(row.get("diagnostic_text")),clean(row.get("remote_mta")),clean(row.get("message_id")),clean(row.get("source")) or "manual",clean(row.get("occurred_at"))))

    def register_batch(self, source: str, path: str, source_hash: str, rows: int) -> None:
        self.batch_id = uid(f"batch:{source}:{source_hash}")
        self.execute("""insert into audit.import_batches(id,organization_id,source_name,source_path,source_sha256,source_rows) values (%s,%s,%s,%s,%s,%s) on conflict (id) do update set source_name=excluded.source_name,source_path=excluded.source_path,source_sha256=excluded.source_sha256,source_rows=excluded.source_rows""", (self.batch_id,ORG_ID,source,path,source_hash,rows))

    def import_sela(self) -> None:
        candidates=json.loads((self.sela_dir/'candidates.json').read_text()); feedback=json.loads((self.sela_dir/'feedback_events.json').read_text()); memory=json.loads((self.sela_dir/'search_memory.json').read_text())
        sources=[('sela/candidates.json',self.sela_dir/'candidates.json',candidates),('sela/feedback_events.json',self.sela_dir/'feedback_events.json',feedback),('sela/search_memory.json',self.sela_dir/'search_memory.json',memory)]
        for source,path,payload in sources:
            values=payload if isinstance(payload,list) else [{"key":k,"value":v} for k,v in payload.items()]
            self.register_batch(source,str(path),sha256(path),len(values))
            for n,row in enumerate(values,1): self.archive(source,clean(row.get('id') or row.get('key') or n),row,n)
        for row in candidates:
            company_id=self.company(row.get('company'),row.get('domain') or row.get('website'),row.get('country'),row.get('city'),row.get('business_type'))
            contact_id=self.email(company_id,row.get('email'),evidence={"source_url":row.get('email_source_url',''),"contact_evidence":row.get('contact_evidence','')})
            prospect_id=uid(f"prospect:{row['id']}"); self.prospects[clean(row['id'])]=prospect_id
            self.execute("""insert into sela.prospects(id,organization_id,company_id,contact_method_id,legacy_candidate_id,campaign,source_run_id,qualification_status,research_status,confidence,do_not_contact,imported_at,updated_at,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,legacy_candidate_id) do update set legacy_payload=excluded.legacy_payload,updated_at=excluded.updated_at""", (prospect_id,ORG_ID,company_id,contact_id,clean(row['id']),clean(row.get('campaign')),clean(row.get('source_run')),clean(row.get('status')),clean(row.get('research_status')),clean(row.get('confidence')),bool(row.get('do_not_contact')),parse_time(row.get('imported_at')),parse_time(row.get('updated_at')),Jsonb(row)))
            self.execute("insert into sela.prospect_research(prospect_id,qualification_method,qualification_reason,reason,angle,supplier_pivot,site_hygiene,research_reason) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (prospect_id) do update set reason=excluded.reason", (prospect_id,clean(row.get('qualification_method')),clean(row.get('qualification_reason')),clean(row.get('reason')),clean(row.get('angle')),clean(row.get('supplier_pivot')),clean(row.get('site_hygiene')),clean(row.get('research_reason'))))
            self.execute("insert into sela.outreach_messages(id,prospect_id,subject,body,message_variant,provider_draft_id,provider_thread_id,provider_message_id,sent_at,last_send_attempt_at,last_send_error,auto_send_blocked,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set legacy_payload=excluded.legacy_payload", (uid(f"sela-message:{row['id']}"),prospect_id,clean(row.get('subject')),clean(row.get('email_draft')),clean(row.get('message_variant')),clean(row.get('gmail_draft_id')),clean(row.get('gmail_thread_id')),clean(row.get('gmail_message_id')),parse_time(row.get('sent_at')),parse_time(row.get('last_gmail_send_attempt_at')),clean(row.get('last_gmail_send_error')),bool(row.get('local_gmail_auto_send_blocked')),Jsonb(row)))
        for n,row in enumerate(feedback,1):
            self.execute("""insert into sela.prospect_events(id,organization_id,prospect_id,legacy_candidate_id,occurred_at,event_type,company_text,campaign,market,business_type,confidence,contact_route,email_type,email_evidence_tier,message_variant,outreach_status_snapshot,detail,legacy_row_number,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,legacy_row_number) do update set legacy_payload=excluded.legacy_payload""", (uid(f"sela-event:{n}"),ORG_ID,self.prospects.get(clean(row.get('candidate_id'))),clean(row.get('candidate_id')),parse_time(row.get('at')) or datetime.now(timezone.utc),clean(row.get('event')),clean(row.get('company')),clean(row.get('campaign')),clean(row.get('market')),clean(row.get('business_type')),clean(row.get('confidence')),clean(row.get('contact_route')),clean(row.get('email_type')),clean(row.get('email_evidence_tier')),clean(row.get('message_variant')),clean(row.get('outreach_status')),clean(row.get('detail')),n,Jsonb(row)))
        activity_path=self.sela_dir/'activity_events.sqlite3'
        con=sqlite3.connect(f"file:{activity_path}?mode=ro",uri=True); con.row_factory=sqlite3.Row
        activity_count=con.execute('select count(*) from activity_events').fetchone()[0]
        self.register_batch('sela/activity_events.sqlite3',str(activity_path),sha256(activity_path),activity_count)
        for row in con.execute('select * from activity_events'):
            row=dict(row); self.archive('sela/activity_events.sqlite3',clean(row['id']),row,row['id']); self.execute("insert into sela.run_activity_events(id,organization_id,legacy_activity_id,run_id,campaign_id,legacy_candidate_id,prospect_id,kind,status,message,details,business_progress,occurred_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,legacy_activity_id) do update set details=excluded.details", (uid(f"sela-activity:{row['id']}"),ORG_ID,row['id'],clean(row.get('run_id')),clean(row.get('campaign_id')),clean(row.get('candidate_id')),self.prospects.get(clean(row.get('candidate_id'))),clean(row.get('kind')),clean(row.get('status')),clean(row.get('message')),Jsonb(json_value(row.get('details_json'))),bool(row.get('business_progress')),parse_time(row.get('created_at')) or datetime.now(timezone.utc)))
        con.close()
        for key,value in memory.items():
            values=value if isinstance(value,list) else [{"key":k,"value":v} for k,v in (value.items() if isinstance(value,dict) else [(key,value)])]
            for n,row in enumerate(values,1): self.execute("insert into sela.search_memory_entries(id,organization_id,entry_type,legacy_row_number,run_id,occurred_at,payload) values (%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,entry_type,legacy_row_number) do update set payload=excluded.payload", (uid(f"memory:{key}:{n}"),ORG_ID,key,n,clean(row.get('run_id')) if isinstance(row,dict) else '',parse_time(row.get('at') if isinstance(row,dict) else ''),Jsonb(row)))

    def import_sela_runs(self) -> None:
        if not self.sela_runs_dir or not self.sela_runs_dir.is_dir():
            return
        manifests = sorted(
            path for path in self.sela_runs_dir.glob("*/run.json") if path.is_file()
        )
        source_name = "sela/runs"
        self.register_batch(
            source_name,
            str(self.sela_runs_dir),
            directory_sha256(self.sela_runs_dir),
            len(manifests),
        )
        for number, path in enumerate(manifests, 1):
            run_id = clean(path.parent.name)
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not run_id:
                self.issue(source_name, run_id or str(number), "INVALID_RUN_MANIFEST", "Run manifest is not an object")
                continue
            self.archive(f"{source_name}/{run_id}/run.json", run_id, manifest, number)
            self.execute("""insert into sela.search_runs
              (id,organization_id,legacy_run_id,status,started_at,completed_at,manifest)
              values (%s,%s,%s,%s,%s,%s,%s)
              on conflict (organization_id,legacy_run_id) do update set
              status=excluded.status,started_at=excluded.started_at,
              completed_at=excluded.completed_at,manifest=excluded.manifest""",
              (uid("run:" + run_id), ORG_ID, run_id, clean(manifest.get("status")),
               parse_time(manifest.get("started_at") or manifest.get("created_at")),
               parse_time(manifest.get("completed_at")), Jsonb(manifest)))

    def run(self) -> dict[str, Any]:
        with psycopg.connect(self.dsn) as con:
            with con.cursor() as cur:
                self.cur=cur; self.ensure_org(); self.import_trosa(); self.import_sela(); self.import_sela_runs()
                for table in ('core.companies','core.people','core.contact_methods','trosa.accounts','trosa.tasks','trosa.timeline_events','trosa.outreach_messages','sela.prospects','sela.prospect_events','sela.run_activity_events','sela.search_memory_entries','audit.legacy_records'):
                    self.report['target'][table]=cur.execute(f'select count(*) from {table}').fetchone()[0]
                self.report['sources'] = {
                    row[0]: {"rows": row[1], "sha256": row[2]}
                    for row in cur.execute("select source_name,source_rows,source_sha256 from audit.import_batches order by source_name")
                }
            con.commit()
        self.report['result']='passed'; return self.report


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url',default=os.environ.get('TRADE_OS_DATABASE_URL','')); parser.add_argument('--trosa-data-dir',type=Path,required=True); parser.add_argument('--sela-data-dir',type=Path,required=True); parser.add_argument('--sela-runs-dir',type=Path); parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if not args.database_url: parser.error('--database-url or TRADE_OS_DATABASE_URL is required')
    runs_dir = args.sela_runs_dir if args.sela_runs_dir is not None else args.sela_data_dir / 'runs'
    report=Importer(args.database_url,args.trosa_data_dir,args.sela_data_dir,runs_dir).run(); args.report.parent.mkdir(parents=True,exist_ok=True); args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n'); print(json.dumps(report,ensure_ascii=False,indent=2,default=str)); return 0


if __name__=='__main__': raise SystemExit(main())
