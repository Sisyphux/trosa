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
    def __init__(self, dsn: str, trosa_dir: Path, sela_dir: Path):
        self.dsn, self.trosa_dir, self.sela_dir = dsn, trosa_dir, sela_dir
        self.report: dict[str, Any] = {"organization_id": str(ORG_ID), "sources": {}, "target": {}, "issues": []}
        self.accounts: dict[str, uuid.UUID] = {}
        self.prospects: dict[str, uuid.UUID] = {}

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.cur.execute(sql, params)

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
        for db_name in ("system.db", "hamid.db", "amy.db", "kelley.db"):
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
                    self.execute("""insert into identity.users(id,organization_id,legacy_user_id,display_name,label,color)
                      values (%s,%s,%s,%s,%s,%s) on conflict (organization_id,legacy_user_id) do update set display_name=excluded.display_name""",
                      (user_id, ORG_ID, clean(row["id"]), clean(row.get("name")), clean(row.get("label")), clean(row.get("color"))))
                    self.execute("insert into identity.memberships(organization_id,user_id,role) values (%s,%s,'member') on conflict do nothing", (ORG_ID, user_id))
                continue
            for row in rows.get("customers", []):
                key = f"{db_name}:customer:{row['id']}"
                company_id = self.company(row.get("company") or row.get("name"), row.get("website"), row.get("country"), business_type=row.get("type"))
                account_id = uid(f"account:{company_id}")
                self.accounts[key] = account_id
                self.execute("""insert into trosa.accounts(id,organization_id,company_id,display_name,account_status,customer_type,channel_type,priority_level,profile,field,industry,company_size,annual_revenue,tags,attention_state,attention_reason,next_follow_up_at,deleted_at,legacy_payload)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (organization_id,company_id) do nothing""",
                  (account_id, ORG_ID, company_id, clean(row.get("name")), clean(row.get("status")), clean(row.get("customer_type")), clean(row.get("type")), clean(row.get("level")), clean(row.get("profile")), clean(row.get("field")), clean(row.get("industry")), clean(row.get("company_size")), clean(row.get("annual_revenue")), clean(row.get("tags")), clean(row.get("attention_state")), clean(row.get("attention_reason")), parse_time(row.get("next_follow_up")), parse_time(row.get("deleted_at")) if clean(row.get("is_deleted")) == "1" else None, Jsonb(row)))
            for row in rows.get("contacts", []):
                account_id = self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}")
                if not account_id: continue
                company_id = self.cur.execute("select company_id from trosa.accounts where id=%s", (account_id,)).fetchone()[0]
                email_value = clean(row.get("email")).lower()
                person_id = uid(f"person:email:{email_value}") if email_value else uid(f"person:{db_name}:{row['id']}")
                self.execute("insert into core.people(id,organization_id,full_name,normalized_name) values (%s,%s,%s,%s) on conflict (id) do nothing", (person_id, ORG_ID, clean(row.get("name")) or "UNKNOWN", norm(row.get("name"))))
                self.execute("insert into core.company_people(id,company_id,person_id,title,source) values (%s,%s,%s,%s,'trosa') on conflict do nothing", (uid(f"company-person:{company_id}:{person_id}:{clean(row.get('title'))}"), company_id, person_id, clean(row.get("title"))))
                self.email(company_id, email_value, person_id, {"source": "trosa.contacts", "legacy_id": row["id"]})
            for row in rows.get("reminders", []):
                account_id=self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}"); due=parse_time(row.get("remind_date"))
                if account_id and due:
                    self.execute("""insert into trosa.tasks(id,account_id,title,content,reason,due_at,status,task_type,manual_order,completed_at,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set legacy_payload=excluded.legacy_payload""", (uid(f"task:{db_name}:{row['id']}"),account_id,clean(row.get("title")),clean(row.get("content")),clean(row.get("reason")),due,'done' if row.get("is_done") else 'open',clean(row.get("reminder_type")),int(row.get("manual_order") or 0),parse_time(row.get("completed_at")),Jsonb(row)))
            for row in rows.get("follow_up_logs", []):
                account_id=self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}"); occurred=parse_time(row.get("follow_date") or row.get("created_at"))
                if account_id and occurred:
                    self.execute("""insert into trosa.timeline_events(id,account_id,event_type,direction,content,result,next_plan,source_module,source_reference,occurred_at,payload) values (%s,%s,%s,%s,%s,%s,%s,'trosa',%s,%s,%s) on conflict (id) do update set payload=excluded.payload""", (uid(f"timeline:{db_name}:{row['id']}"),account_id,clean(row.get("activity_type")),clean(row.get("direction")),clean(row.get("content")),clean(row.get("result")),clean(row.get("next_plan")),f"{db_name}:{row['id']}",occurred,Jsonb(row)))
            for row in rows.get("outreach_emails", []):
                account_id=self.accounts.get(f"{db_name}:customer:{row.get('customer_id')}")
                if account_id:
                    self.execute("""insert into trosa.outreach_messages(id,account_id,subject,body,sent_at,reply_status,reply_content,reply_at,provider,provider_message_id,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,'legacy',%s,%s) on conflict (id) do update set legacy_payload=excluded.legacy_payload""", (uid(f"trosa-message:{db_name}:{row['id']}"),account_id,clean(row.get("subject")),clean(row.get("content")),parse_time(row.get("sent_date")),clean(row.get("reply_status")),clean(row.get("reply_content")),parse_time(row.get("reply_date")),clean(row.get("external_id")),Jsonb(row)))

    def register_batch(self, source: str, path: str, source_hash: str, rows: int) -> None:
        self.batch_id = uid(f"batch:{source}:{source_hash}")
        self.execute("""insert into audit.import_batches(id,organization_id,source_name,source_path,source_sha256,source_rows) values (%s,%s,%s,%s,%s,%s) on conflict (organization_id,source_path,source_sha256) do update set source_rows=excluded.source_rows""", (self.batch_id,ORG_ID,source,path,source_hash,rows))

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

    def run(self) -> dict[str, Any]:
        with psycopg.connect(self.dsn) as con:
            with con.cursor() as cur:
                self.cur=cur; self.ensure_org(); self.import_trosa(); self.import_sela()
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
    parser.add_argument('--database-url',default=os.environ.get('TRADE_OS_DATABASE_URL','')); parser.add_argument('--trosa-data-dir',type=Path,required=True); parser.add_argument('--sela-data-dir',type=Path,required=True); parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if not args.database_url: parser.error('--database-url or TRADE_OS_DATABASE_URL is required')
    report=Importer(args.database_url,args.trosa_data_dir,args.sela_data_dir).run(); args.report.parent.mkdir(parents=True,exist_ok=True); args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n'); print(json.dumps(report,ensure_ascii=False,indent=2,default=str)); return 0


if __name__=='__main__': raise SystemExit(main())
