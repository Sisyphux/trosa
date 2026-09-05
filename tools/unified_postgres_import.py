#!/usr/bin/env python3
"""Repeatable, non-production importer for the unified Trade OS PostgreSQL DB.

Sources are opened read-only. Every source row is archived in
audit.legacy_records before the business projections below are written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
LOCAL_TIMEZONE = timezone(timedelta(hours=8))


def uid(value: str) -> uuid.UUID:
    return uuid.uuid5(NS, value)


def compat_uuid(seed: str) -> uuid.UUID:
    """Return the exact UUID produced by PostgreSQL ``trosa.compat_uuid``.

    The compatibility triggers use an MD5-derived UUID (rather than the
    importer namespace UUID) for rows whose canonical projection can later be
    revisited through a legacy-shaped view.  Keeping the algorithm here makes
    a one-time import and a later compatibility write converge on the same
    primary key instead of creating a second fact.
    """
    digest = hashlib.md5(clean(seed).encode("utf-8")).hexdigest()
    return uuid.UUID(digest)


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean(value).lower())).strip()


def domain(value: Any) -> str:
    value = clean(value).lower()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.netloc.split("@")[-1].split(":")[0].removeprefix("www.")


def legacy_user_key(db_name: str) -> str:
    """Return the canonical lower-case owner key used by PostgreSQL views."""
    return Path(db_name).stem.strip().lower()


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
            # Keep an offset that was already present.  Appending +08:00 to
            # ``2026-09-04 12:00:00+00:00`` would create an invalid double
            # offset and abort an otherwise recoverable import row.
            text = text.replace(" ", "T", 1)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        # SQLite legacy rows often contain an ISO ``T`` timestamp without an
        # offset.  PostgreSQL would interpret a naive timestamptz according to
        # the session timezone, which is not a safe assumption for a server.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed
    except (OverflowError, ValueError):
        return None


def legacy_int(value: Any, default: int | None = None) -> int | None:
    """Parse a SQLite numeric field without Python's permissive coercions."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else default
    text = clean(value)
    if not re.fullmatch(r"[+-]?\d+", text):
        return default
    try:
        return int(text)
    except (OverflowError, ValueError):
        return default


def legacy_bool(value: Any, default: bool = False) -> bool:
    """Read integer, boolean, and textual SQLite flags consistently."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if math.isfinite(value) else default
    text = clean(value).casefold()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"", "0", "false", "no", "off", "n", "none", "null"}:
        return False if text else default
    return default


def compat_dedupe_key(legacy_user: str, value: Any) -> str:
    """Namespace per-user SQLite keys before storing them in shared PG."""
    raw = clean(value)
    return f"compat:{legacy_user}:{raw}" if raw else ""


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


def sela_evidence_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the evidence shapes used by historical Sela exports.

    Older exports used a list, a single source object, or a JSON-encoded value;
    some only carried the email-source fields on the candidate itself.  The
    importer keeps each source as a separate canonical evidence row and never
    turns a malformed value into a guessed source.
    """
    raw = json_value(row.get("evidence"))
    if isinstance(raw, dict):
        nested = raw.get("items") or raw.get("sources")
        values = nested if isinstance(nested, list) else [raw]
    elif isinstance(raw, list):
        values = raw
    elif clean(raw):
        values = [raw]
    else:
        values = []

    if clean(row.get("email_source_url")):
        values.append({
            "evidence_type": "email_source",
            "source_url": row.get("email_source_url"),
            "excerpt": row.get("email_evidence") or row.get("contact_evidence") or "",
        })
    elif clean(row.get("contact_evidence")):
        values.append({
            "evidence_type": "contact_source",
            "excerpt": row.get("contact_evidence"),
        })
    if not values and clean(row.get("website")):
        values.append({
            "evidence_type": "candidate_website",
            "source_url": row.get("website"),
        })

    entries: list[dict[str, Any]] = []
    for value in values:
        item = value if isinstance(value, dict) else {"value": value}
        evidence_type = clean(
            item.get("evidence_type") or item.get("type") or item.get("kind")
        ) or "candidate_source"
        source_url = clean(item.get("source_url") or item.get("url") or item.get("href"))
        source_file = clean(item.get("source_file") or item.get("file"))
        excerpt = clean(
            item.get("excerpt") or item.get("text") or item.get("detail")
            or item.get("description") or item.get("value")
        )
        captured_at = parse_time(
            item.get("captured_at") or item.get("at")
            or row.get("captured_at") or row.get("updated_at")
        )
        entries.append({
            "evidence_type": evidence_type,
            "source_url": source_url,
            "source_file": source_file,
            "excerpt": excerpt,
            "captured_at": captured_at,
            "raw_payload": item,
        })
    return entries


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
        self.user_ids: dict[str, uuid.UUID] = {}

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.cur.execute(sql, params)

    @staticmethod
    def legacy_id(value: Any) -> int | None:
        number = legacy_int(value)
        return number if number is not None and number > 0 else None

    def identity_user_id(self, legacy_user: Any) -> uuid.UUID | None:
        """Resolve the target identity row instead of guessing its UUID."""
        username = clean(legacy_user).lower()
        if not username:
            return None
        if username in self.user_ids:
            return self.user_ids[username]
        row = self.cur.execute(
            """select id from identity.users
               where organization_id=%s and (legacy_user_id=%s or username=%s)
               order by case when legacy_user_id=%s then 0 else 1 end
               limit 1""",
            (ORG_ID, username, username, username),
        ).fetchone()
        if not row:
            return None
        self.user_ids[username] = row[0]
        return row[0]

    def account_ref(self, db_name: str, customer_id: Any, account_id: uuid.UUID) -> None:
        legacy_customer_id = self.legacy_id(customer_id)
        if legacy_customer_id is None:
            return
        legacy_user = legacy_user_key(db_name)
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
          (ORG_ID, legacy_user_key(db_name), table_name, number, target_id))

    def account_for(self, db_name: str, customer_id: Any) -> uuid.UUID | None:
        legacy_customer_id = self.legacy_id(customer_id)
        if legacy_customer_id is None:
            return None
        return self.accounts.get(f"{db_name}:customer:{legacy_customer_id}")

    def ref_target(self, db_name: str, table_name: str, legacy_id: Any) -> uuid.UUID | None:
        number = self.legacy_id(legacy_id)
        if number is None:
            return None
        row = self.cur.execute(
            """select target_id from trosa.legacy_row_refs
               where organization_id=%s and legacy_user_id=%s and table_name=%s and legacy_id=%s""",
            (ORG_ID, legacy_user_key(db_name), table_name, number),
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

    def contact_method_for(
        self, db_name: str, contact_id: Any, email_value: Any = None
    ) -> uuid.UUID | None:
        """Resolve a legacy contact without crossing the user namespace."""
        number = self.legacy_id(contact_id)
        if number is not None:
            row = self.cur.execute(
                """select contact_method_id from trosa.contact_legacy_refs
                   where organization_id=%s and legacy_user_id=%s
                     and legacy_contact_id=%s limit 1""",
                (ORG_ID, legacy_user_key(db_name), number),
            ).fetchone()
            if row:
                return row[0]
        return self.person_method_for_email(email_value)

    def issue(self, source: str, key: str, code: str, detail: str, payload: Any = None) -> None:
        issue_id = uid(f"issue:{self.batch_id}:{source}:{key}:{code}")
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
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        payload_sha256 = hashlib.sha256(encoded).hexdigest()
        archive_key = key
        existing = self.cur.execute(
            """select payload_sha256,legacy_row_number from audit.legacy_records
               where batch_id=%s and source_table=%s and legacy_key=%s limit 1""",
            (self.batch_id, source, archive_key),
        ).fetchone()
        if existing and (
            existing[0] != payload_sha256
            or (row_number is not None and existing[1] != row_number)
        ):
            suffix = f"#row-{row_number}" if row_number is not None else "#duplicate"
            archive_key = f"{key}{suffix}"
            duplicate_number = 2
            while self.cur.execute(
                """select 1 from audit.legacy_records
                   where batch_id=%s and source_table=%s and legacy_key=%s""",
                (self.batch_id, source, archive_key),
            ).fetchone():
                archive_key = f"{key}{suffix}-{duplicate_number}"
                duplicate_number += 1
            self.issue(
                source,
                str(key),
                "DUPLICATE_LEGACY_KEY",
                f"Duplicate legacy key was retained under archived key {archive_key}",
                row,
            )
        record_id = uid(f"raw:{self.batch_id}:{source}:{archive_key}")
        self.execute("""insert into audit.legacy_records
          (id,batch_id,source_table,legacy_key,legacy_row_number,payload,payload_sha256)
          values (%s,%s,%s,%s,%s,%s,%s)
          on conflict (batch_id,source_table,legacy_key) do update set payload=excluded.payload,payload_sha256=excluded.payload_sha256""",
          (record_id, self.batch_id, source, archive_key, row_number, Jsonb(payload), payload_sha256))

    def company(
        self,
        name: Any,
        website: Any,
        country: Any,
        city: Any = "",
        business_type: Any = "",
        source_identity: Any = "",
    ) -> uuid.UUID:
        canonical, normalized, dom = clean(name), norm(name), domain(website)
        identity = f"domain:{dom}" if dom else f"name:{normalized}|{norm(country)}"
        # Use the same deterministic key as the runtime compatibility writer.
        # A later legacy-shaped write must converge on this company instead
        # of creating a second canonical fact after a rehearsal import.
        company_id = compat_uuid(f"company:{identity}")
        if not canonical:
            canonical, normalized = "UNKNOWN", "unknown"
            self.issue("core.companies", identity, "MISSING_COMPANY_NAME", "Preserved only as an unresolved identity")
        source_key = clean(source_identity) or identity
        matches = []
        if dom:
            matches = self.cur.execute(
                """select d.company_id from core.company_domains d
                   join core.companies c on c.id=d.company_id
                  where c.organization_id=%s and d.normalized_domain=%s
                  order by d.is_primary desc, d.created_at asc""",
                (ORG_ID, dom),
            ).fetchall()
            ambiguous_match = len(matches) > 1
            if ambiguous_match:
                # Never choose an arbitrary company when a damaged/partially
                # migrated database contains multiple exact candidates.  Keep
                # a deterministic source-scoped candidate and route the
                # conflict to the migration review queue instead of silently
                # merging facts.
                candidate_ids = [str(row[0]) for row in matches]
                self.issue(
                    "core.companies",
                    source_key,
                    "AMBIGUOUS_COMPANY_MATCH",
                    "Multiple exact company identities were found; retained a source-scoped candidate for review",
                    {
                        "name": canonical,
                        "website": clean(website),
                        "country": clean(country),
                        "candidate_company_ids": candidate_ids,
                    },
                )
                company_id = compat_uuid(f"company-candidate:{source_key}")
            elif matches:
                # A previous rehearsal/import may have created the same
                # business identity with a different UUID.  Always continue
                # with the database's actual key so later foreign keys cannot
                # point at a merely deterministic, non-existent row.
                company_id = matches[0][0]
        else:
            matches = self.cur.execute(
                """select id from core.companies
                   where organization_id=%s and normalized_name=%s and country_code=%s
                   order by created_at asc""",
                (ORG_ID, normalized, clean(country)),
            ).fetchall()
            # A name/country match is a candidate, not a confirmed identity.
            # Keep it source-scoped even when there is only one existing row;
            # the importer must not silently merge same-name companies.
            self.issue(
                "core.companies",
                source_key,
                "COMPANY_MATCH_REVIEW",
                "No exact domain identity was available; retained a source-scoped name/country candidate for review",
                {
                    "name": canonical,
                    "website": clean(website),
                    "country": clean(country),
                    "candidate_company_ids": [str(row[0]) for row in matches],
                },
            )
            ambiguous_match = True
            company_id = compat_uuid(f"company-candidate:{source_key}")
        self.execute("""insert into core.companies
          (id,organization_id,canonical_name,normalized_name,website,country_code,city,business_type,identity_status)
          values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
          on conflict (id) do update set canonical_name=excluded.canonical_name,
            normalized_name=excluded.normalized_name,website=excluded.website,
            country_code=excluded.country_code,city=excluded.city,
            business_type=excluded.business_type,
            identity_status=case when excluded.identity_status='review'
                                then 'review' else core.companies.identity_status end,
            updated_at=now()""",
          (company_id, ORG_ID, canonical, normalized, clean(website), clean(country), clean(city),
           clean(business_type), 'review' if ambiguous_match else 'confirmed'))
        if dom:
            domain_seed = f"company-domain:{dom}"
            if ambiguous_match:
                domain_seed = f"company-domain-candidate:{source_key}:{dom}"
            domain_id = compat_uuid(domain_seed)
            self.execute("""insert into core.company_domains(id,company_id,normalized_domain,source_url,is_primary,verification_status)
              values (%s,%s,%s,%s,%s,%s)
              on conflict (company_id,normalized_domain) do update set
                source_url=excluded.source_url,is_primary=excluded.is_primary,
                verification_status=excluded.verification_status""",
              (domain_id, company_id, dom, clean(website), not ambiguous_match,
               'review' if ambiguous_match else 'imported'))
        return company_id

    def email(self, company_id: uuid.UUID, value: Any, person_id: uuid.UUID | None = None, evidence: Any = None) -> uuid.UUID | None:
        value = clean(value).lower()
        if not value or "@" not in value:
            return None
        method_id = compat_uuid(f"email:{value}")
        self.execute("""insert into core.contact_methods
          (id,organization_id,company_id,person_id,kind,value,normalized_value,evidence)
          values (%s,%s,%s,%s,'email',%s,%s,%s)
          on conflict (organization_id,kind,normalized_value) do update set
            person_id=coalesce(core.contact_methods.person_id,excluded.person_id),
            company_id=case when coalesce(core.contact_methods.person_id,excluded.person_id) is null
                            then core.contact_methods.company_id else null end,
            updated_at=now()""",
          (method_id, ORG_ID, None if person_id else company_id, person_id, value, value, Jsonb(evidence or {})))
        actual = self.cur.execute(
            """select id from core.contact_methods
               where organization_id=%s and kind='email' and normalized_value=%s""",
            (ORG_ID, value),
        ).fetchone()
        return actual[0] if actual else method_id

    def import_trosa_system_rows(
        self, db_name: str, rows: dict[str, list[dict[str, Any]]], source_name: str
    ) -> None:
        """Project the non-user rows that live in the legacy system database.

        ``system.db`` is not just an identity registry.  It also contains
        settings, invitations, and historical weekly reports.  The archive
        loop above preserves every source row, while this method keeps the
        operational projections available after a PostgreSQL cutover.
        """
        for row in rows.get("app_settings", []):
            key = clean(row.get("key"))
            if not key:
                self.issue(
                    f"{source_name}/app_settings",
                    clean(row.get("id")),
                    "INVALID_SETTING_KEY",
                    "Application setting has no key and was kept in audit only",
                    row,
                )
                continue
            self.execute(
                """insert into trade_os_compat.app_settings(key,value,updated_at)
                   values (%s,%s,%s)
                   on conflict (key) do update set value=excluded.value,
                   updated_at=excluded.updated_at""",
                (key, clean(row.get("value")), clean(row.get("updated_at"))),
            )

        for row in rows.get("weekly_reports", []):
            legacy_user = clean(row.get("user_id")).lower()
            week_start = clean(row.get("week_start"))
            row_key = clean(row.get("id")) or week_start or "unknown"
            if not legacy_user:
                self.issue(
                    f"{source_name}/weekly_reports",
                    row_key,
                    "INVALID_REPORT_USER",
                    "Weekly report has no user id and was kept in audit only",
                    row,
                )
                continue
            if self.identity_user_id(legacy_user) is None:
                self.issue(
                    f"{source_name}/weekly_reports",
                    row_key,
                    "UNKNOWN_REPORT_USER",
                    "Weekly report user is not present in the identity registry; projection was skipped and the raw owner was preserved",
                    row,
                )
                continue
            if not week_start:
                self.issue(
                    f"{source_name}/weekly_reports",
                    row_key,
                    "INVALID_REPORT_WEEK",
                    "Weekly report has no week_start and was kept in audit only",
                    row,
                )
                continue
            status = clean(row.get("status")) or "draft"
            if status not in {"draft", "submitted"}:
                self.issue(
                    f"{source_name}/weekly_reports",
                    row_key,
                    "INVALID_REPORT_STATUS",
                    "Weekly report status was normalized to draft",
                    row,
                )
                status = "draft"
            created_at = parse_time(row.get("created_at"))
            updated_at = parse_time(row.get("updated_at"))
            if clean(row.get("created_at")) and created_at is None:
                self.issue(
                    f"{source_name}/weekly_reports",
                    row_key,
                    "INVALID_REPORT_CREATED_AT",
                    "Invalid report creation time; current time used for the required target field",
                    row,
                )
            if clean(row.get("updated_at")) and updated_at is None:
                self.issue(
                    f"{source_name}/weekly_reports",
                    row_key,
                    "INVALID_REPORT_UPDATED_AT",
                    "Invalid report update time; creation time/current time used",
                    row,
                )
            created_at = created_at or datetime.now(timezone.utc)
            updated_at = updated_at or created_at
            report_id = self.legacy_id(row.get("id"))
            natural = self.cur.execute(
                """select id from trosa.weekly_reports
                   where organization_id=%s and legacy_user_id=%s and week_start=%s
                   limit 1""",
                (ORG_ID, legacy_user, week_start),
            ).fetchone()
            if natural:
                report_id = natural[0]
            elif report_id is not None:
                collision = self.cur.execute(
                    "select organization_id,legacy_user_id,week_start from trosa.weekly_reports where id=%s",
                    (report_id,),
                ).fetchone()
                if collision:
                    self.issue(
                        f"{source_name}/weekly_reports",
                        row_key,
                        "REPORT_ID_COLLISION",
                        "Legacy report id is already used by another report; PostgreSQL generated a new id",
                        row,
                    )
                    report_id = None
            report_fields = (
                ORG_ID,
                legacy_user,
                week_start,
                clean(row.get("content")),
                clean(row.get("highlights")),
                clean(row.get("challenges")),
                clean(row.get("next_plan")),
                status,
                created_at,
                updated_at,
            )
            if report_id is None:
                self.execute(
                    """insert into trosa.weekly_reports
                       (organization_id,legacy_user_id,week_start,content,highlights,
                        challenges,next_plan,status,created_at,updated_at)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       on conflict (organization_id,legacy_user_id,week_start) do update set
                        content=excluded.content,highlights=excluded.highlights,
                        challenges=excluded.challenges,next_plan=excluded.next_plan,
                        status=excluded.status,updated_at=excluded.updated_at""",
                    report_fields,
                )
            else:
                self.execute(
                    """insert into trosa.weekly_reports
                       (id,organization_id,legacy_user_id,week_start,content,highlights,
                        challenges,next_plan,status,created_at,updated_at)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       on conflict (organization_id,legacy_user_id,week_start) do update set
                        content=excluded.content,highlights=excluded.highlights,
                        challenges=excluded.challenges,next_plan=excluded.next_plan,
                        status=excluded.status,updated_at=excluded.updated_at""",
                    (report_id, *report_fields),
                )

        # Explicit legacy ids keep the compatibility view stable.  Advance
        # PostgreSQL's identity sequence as well, otherwise the next new
        # report could reuse an imported primary key after the cutover.
        if rows.get("weekly_reports"):
            self.execute(
                """select setval(
                         pg_get_serial_sequence('trosa.weekly_reports','id'),
                         coalesce((select max(id) from trosa.weekly_reports), 0) + 1,
                         false
                       )"""
            )

        for row in rows.get("team_invitations", []):
            invitation_id = clean(row.get("id"))
            token_hash = clean(row.get("token_hash"))
            created_by = clean(row.get("created_by")).lower()
            created_at = clean(row.get("created_at"))
            expires_at = clean(row.get("expires_at"))
            row_key = invitation_id or token_hash or "unknown"
            missing = [
                name
                for name, value in (
                    ("id", invitation_id),
                    ("token_hash", token_hash),
                    ("created_by", created_by),
                    ("created_at", created_at),
                    ("expires_at", expires_at),
                )
                if not value
            ]
            if missing:
                self.issue(
                    f"{source_name}/team_invitations",
                    row_key,
                    "INVALID_INVITATION",
                    "Invitation is missing required fields: " + ", ".join(missing),
                    row,
                )
                continue
            if parse_time(expires_at) is None:
                self.issue(
                    f"{source_name}/team_invitations",
                    row_key,
                    "INVALID_INVITATION_EXPIRY",
                    "Invitation expiry is not a valid ISO timestamp; it remains archived but cannot be used",
                    row,
                )
            existing_id = self.cur.execute(
                """select organization_id from identity.team_invitations
                   where id=%s limit 1""",
                (invitation_id,),
            ).fetchone()
            if existing_id and existing_id[0] != ORG_ID:
                self.issue(
                    f"{source_name}/team_invitations",
                    row_key,
                    "INVITATION_ORGANIZATION_COLLISION",
                    "Invitation id already belongs to another organization; projection was skipped",
                    row,
                )
                continue
            existing_token = self.cur.execute(
                """select organization_id from identity.team_invitations
                   where token_hash=%s and id<>%s limit 1""",
                (token_hash, invitation_id),
            ).fetchone()
            if existing_token:
                self.issue(
                    f"{source_name}/team_invitations",
                    row_key,
                    "INVITATION_TOKEN_COLLISION",
                    "Invitation token is already used by another row; projection was skipped",
                    row,
                )
                continue
            self.execute(
                """insert into identity.team_invitations
                   (id,organization_id,token_hash,created_by,created_at,expires_at,
                    accepted_at,accepted_user_id,revoked_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (id) do update set organization_id=excluded.organization_id,
                    token_hash=excluded.token_hash,created_by=excluded.created_by,
                    created_at=excluded.created_at,expires_at=excluded.expires_at,
                    accepted_at=excluded.accepted_at,accepted_user_id=excluded.accepted_user_id,
                    revoked_at=excluded.revoked_at""",
                (
                    invitation_id,
                    ORG_ID,
                    token_hash,
                    created_by,
                    created_at,
                    expires_at,
                    clean(row.get("accepted_at")),
                    clean(row.get("accepted_user_id")),
                    clean(row.get("revoked_at")),
                ),
            )

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
                    self.archive(f"{source_name}/{table}", clean(row.get("id")) or str(number), row, number)
            if db_name == "system.db":
                for row in rows.get("users", []):
                    legacy_id = clean(row.get("id")).lower()
                    if not legacy_id:
                        self.issue(source_name + "/users", str(row.get("id", "")), "INVALID_USER_ID", "User row has no legacy id", row)
                        continue
                    user_id = uid(f"user:{legacy_id}")
                    username = clean(row.get("username")).lower() or legacy_id
                    role = clean(row.get("role")) or ('admin' if username == 'hamid' else 'member')
                    self.execute("""insert into identity.users
                      (id,organization_id,legacy_user_id,username,display_name,label,color,password_hash,role,created_by,active,legacy_payload)
                      values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      on conflict (organization_id,legacy_user_id) do update set
                        username=excluded.username,display_name=excluded.display_name,label=excluded.label,color=excluded.color,
                       password_hash=excluded.password_hash,role=excluded.role,created_by=excluded.created_by,active=excluded.active,
                        legacy_payload=excluded.legacy_payload,updated_at=now()""",
                      (user_id, ORG_ID, legacy_id, username,
                       clean(row.get("name")), clean(row.get("label")), clean(row.get("color")),
                       clean(row.get("password_hash")), role, clean(row.get("created_by")),
                       legacy_bool(row.get("active"), True), Jsonb(row)))
                    actual_user_id = self.identity_user_id(legacy_id) or user_id
                    self.user_ids[legacy_id.lower()] = actual_user_id
                    self.user_ids[username] = actual_user_id
                    self.execute("insert into identity.memberships(organization_id,user_id,role) values (%s,%s,%s) on conflict (organization_id,user_id) do update set role=excluded.role", (ORG_ID, actual_user_id, role))
                self.import_trosa_system_rows(db_name, rows, source_name)
                continue
            user = legacy_user_key(db_name)
            for row in rows.get("customers", []):
                legacy_customer_id = self.legacy_id(row.get("id"))
                if legacy_customer_id is None:
                    self.issue(source_name + "/customers", clean(row.get("id")), "INVALID_CUSTOMER_ID", "Customer row has no positive integer id", row)
                    continue
                key = f"{db_name}:customer:{legacy_customer_id}"
                company_id = self.company(
                    row.get("company") or row.get("name"),
                    row.get("website"),
                    row.get("country"),
                    business_type=row.get("type"),
                    source_identity=f"trosa:{user}:customer:{legacy_customer_id}",
                )
                account_id = compat_uuid(f"account:{company_id}")
                owner_user_id = self.identity_user_id(user)
                self.execute("""insert into trosa.accounts(id,organization_id,company_id,owner_user_id,display_name,account_status,customer_type,channel_type,priority_level,profile,field,industry,company_size,annual_revenue,tags,attention_state,attention_reason,last_contact_at,next_follow_up_at,deleted_at,legacy_payload)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (organization_id,company_id) do nothing""",
                  (account_id, ORG_ID, company_id, owner_user_id, clean(row.get("name")), clean(row.get("status")), clean(row.get("customer_type")), clean(row.get("type")), clean(row.get("level")), clean(row.get("profile")), clean(row.get("field")), clean(row.get("industry")), clean(row.get("company_size")), clean(row.get("annual_revenue")), clean(row.get("tags")), clean(row.get("attention_state")), clean(row.get("attention_reason")), parse_time(row.get("last_contact")), parse_time(row.get("next_follow_up")), parse_time(row.get("deleted_at")) if legacy_bool(row.get("is_deleted")) else None, Jsonb(row)))
                actual_account = self.cur.execute(
                    "select id from trosa.accounts where organization_id=%s and company_id=%s",
                    (ORG_ID, company_id),
                ).fetchone()
                if not actual_account:
                    self.issue(source_name + "/customers", str(legacy_customer_id), "ACCOUNT_NOT_CREATED", "Customer account projection could not be resolved", row)
                    continue
                account_id = actual_account[0]
                self.accounts[key] = account_id
                self.account_ref(db_name, legacy_customer_id, account_id)
            for row in rows.get("contacts", []):
                legacy_contact_id = self.legacy_id(row.get("id"))
                legacy_customer_id = self.legacy_id(row.get("customer_id"))
                account_id = self.accounts.get(f"{db_name}:customer:{legacy_customer_id}")
                if not account_id:
                    self.issue(source_name + "/contacts", clean(row.get("id")), "MISSING_CUSTOMER", "Contact kept in audit only because its customer is unavailable", row)
                    continue
                if legacy_contact_id is None or legacy_customer_id is None:
                    self.issue(source_name + "/contacts", clean(row.get("id")), "INVALID_CONTACT_ID", "Contact has an invalid legacy id or customer id", row)
                    continue
                account_row = self.cur.execute("select company_id from trosa.accounts where id=%s", (account_id,)).fetchone()
                if not account_row:
                    self.issue(source_name + "/contacts", str(legacy_contact_id), "ACCOUNT_NOT_FOUND", "Contact account projection is missing", row)
                    continue
                company_id = account_row[0]
                email_value = clean(row.get("email")).lower()
                if email_value:
                    existing_person = self.cur.execute(
                        """select person_id from core.contact_methods
                           where organization_id=%s and kind='email' and normalized_value=%s
                           limit 1""",
                        (ORG_ID, email_value),
                    ).fetchone()
                else:
                    existing_person = None
                person_id = (existing_person[0] if existing_person else
                             (compat_uuid(f"person:email:{email_value}") if email_value else
                              compat_uuid(f"person:{user}:{legacy_contact_id}")))
                self.execute("insert into core.people(id,organization_id,full_name,normalized_name) values (%s,%s,%s,%s) on conflict (id) do update set full_name=case when core.people.full_name='' or core.people.full_name='UNKNOWN' then excluded.full_name else core.people.full_name end,updated_at=now()", (person_id, ORG_ID, clean(row.get("name")) or "UNKNOWN", norm(row.get("name"))))
                self.execute("insert into core.company_people(id,company_id,person_id,title,source) values (%s,%s,%s,%s,'trosa') on conflict do nothing", (compat_uuid(f"company-person:{company_id}:{person_id}:{clean(row.get('title'))}"), company_id, person_id, clean(row.get("title"))))
                contact_method_id = self.email(company_id, email_value, person_id, {"source": "trosa.contacts", "legacy_id": legacy_contact_id})
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
                      (ORG_ID, user, legacy_contact_id, legacy_customer_id, account_id,
                       person_id, contact_method_id, clean(row.get("name")), clean(row.get("title")),
                       clean(row.get("phone")), clean(row.get("whatsapp")), clean(row.get("linkedin")),
                       clean(row.get("preferred_channel")), clean(row.get("contact_type")) or "person",
                       legacy_bool(row.get("is_primary")), clean(row.get("notes")), Jsonb(row)))
            for row in rows.get("reminders", []):
                legacy_reminder_id = self.legacy_id(row.get("id"))
                account_id = self.accounts.get(f"{db_name}:customer:{self.legacy_id(row.get('customer_id'))}")
                due = parse_time(row.get("remind_date"))
                if not account_id:
                    self.issue(source_name + "/reminders", clean(row.get("id")), "MISSING_CUSTOMER", "Reminder kept in audit only because its customer is unavailable", row)
                    continue
                if legacy_reminder_id is None:
                    self.issue(source_name + "/reminders", clean(row.get("id")), "INVALID_REMINDER_ID", "Reminder has no positive integer id", row)
                    continue
                if due is None:
                    self.issue(source_name + "/reminders", str(legacy_reminder_id), "INVALID_DUE_DATE", "Reminder was not projected because its required due date is invalid", row)
                    continue
                target_id = compat_uuid(f"task:{user}:{legacy_reminder_id}")
                self.execute("""insert into trosa.tasks(id,account_id,title,content,reason,due_at,status,task_type,source_activity_legacy_id,manual_order,completed_at,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set source_activity_legacy_id=excluded.source_activity_legacy_id,legacy_payload=excluded.legacy_payload""", (target_id,account_id,clean(row.get("title")),clean(row.get("content")),clean(row.get("reason")),due,'done' if legacy_bool(row.get("is_done")) else 'open',clean(row.get("reminder_type")),clean(row.get("source_activity_id")),legacy_int(row.get("manual_order"), 0),parse_time(row.get("completed_at")),Jsonb(row)))
                self.row_ref(db_name, "reminders", legacy_reminder_id, target_id)
            for row in rows.get("follow_up_logs", []):
                legacy_activity_id = self.legacy_id(row.get("id"))
                account_id = self.accounts.get(f"{db_name}:customer:{self.legacy_id(row.get('customer_id'))}")
                occurred = parse_time(row.get("follow_date") or row.get("created_at"))
                if not account_id:
                    self.issue(source_name + "/follow_up_logs", clean(row.get("id")), "MISSING_CUSTOMER", "Communication kept in audit only because its customer is unavailable", row)
                    continue
                if legacy_activity_id is None:
                    self.issue(source_name + "/follow_up_logs", clean(row.get("id")), "INVALID_ACTIVITY_ID", "Communication has no positive integer id", row)
                    continue
                if occurred is None:
                    self.issue(source_name + "/follow_up_logs", str(legacy_activity_id), "INVALID_ACTIVITY_DATE", "Communication was not projected because it has no valid timestamp", row)
                    continue
                target_id = compat_uuid(f"timeline:{user}:{legacy_activity_id}")
                contact_method_id = self.contact_method_for(db_name, row.get("contact_id"))
                self.execute("""insert into trosa.timeline_events
                  (id,account_id,contact_method_id,event_type,direction,content,result,next_plan,
                   source_module,source_reference,occurred_at,payload)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,'trosa',%s,%s,%s)
                  on conflict (id) do update set contact_method_id=excluded.contact_method_id,
                   payload=excluded.payload""", (target_id,account_id,contact_method_id,
                   clean(row.get("activity_type")),clean(row.get("direction")),clean(row.get("content")),
                   clean(row.get("result")),clean(row.get("next_plan")),f"{db_name}:{legacy_activity_id}",
                   occurred,Jsonb(row)))
                self.row_ref(db_name, "follow_up_logs", legacy_activity_id, target_id)
            for row in rows.get("outreach_emails", []):
                legacy_outreach_id = self.legacy_id(row.get("id"))
                account_id = self.accounts.get(f"{db_name}:customer:{self.legacy_id(row.get('customer_id'))}")
                if not account_id:
                    self.issue(source_name + "/outreach_emails", clean(row.get("id")), "MISSING_CUSTOMER", "Outreach record kept in audit only because its customer is unavailable", row)
                    continue
                if legacy_outreach_id is None:
                    self.issue(source_name + "/outreach_emails", clean(row.get("id")), "INVALID_OUTREACH_ID", "Outreach record has no positive integer id", row)
                    continue
                target_id = compat_uuid(f"trosa-message:{user}:{legacy_outreach_id}")
                contact_method_id = self.contact_method_for(db_name, row.get("contact_id"), row.get("recipient_email"))
                self.execute("""insert into trosa.outreach_messages
                  (id,account_id,contact_method_id,subject,body,sent_at,reply_status,reply_content,
                   reply_at,provider,provider_message_id,legacy_payload)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'legacy',%s,%s)
                  on conflict (id) do update set contact_method_id=excluded.contact_method_id,
                   legacy_payload=excluded.legacy_payload""",
                  (target_id,account_id,contact_method_id,clean(row.get("subject")),clean(row.get("content")),
                   parse_time(row.get("sent_date")),clean(row.get("reply_status")),clean(row.get("reply_content")),
                   parse_time(row.get("reply_date")),clean(row.get("external_id")),Jsonb(row)))
                self.row_ref(db_name, "outreach_emails", legacy_outreach_id, target_id)

            self.import_trosa_secondary(db_name, rows)

    def import_trosa_secondary(self, db_name: str, rows: dict[str, list[dict[str, Any]]]) -> None:
        """Project the remaining Trosa tables without recreating core entities."""
        user = legacy_user_key(db_name)
        source_name = f"trosa/{db_name}"

        def account_id(row: dict[str, Any]) -> uuid.UUID | None:
            return self.account_for(db_name, row.get("customer_id"))

        def required_account(row: dict[str, Any], table: str) -> uuid.UUID | None:
            account = account_id(row)
            if not account:
                self.issue(f"{source_name}/{table}", clean(row.get("id")), "MISSING_CUSTOMER", "Row kept in audit only because its customer is unavailable", row)
            return account

        # Research and customer AI state use the normalized module tables.
        for row in rows.get("research_reports", []):
            account = required_account(row, "research_reports")
            if not account:
                continue
            target = self.ref_target(db_name, "research_reports", row.get("id"))
            if not target:
                target = self.cur.execute("select id from trosa.research_reports where account_id=%s", (account,)).fetchone()
                target = target[0] if target else compat_uuid(f"research:{user}:{row.get('id')}")
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
            account = required_account(row, "external_analysis_notes")
            if not account:
                continue
            target = compat_uuid(f"external-note:{user}:{row.get('id')}")
            self.execute("""insert into trosa.external_analysis_notes
              (id,account_id,content,source,legacy_payload,created_at,updated_at)
              values (%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set content=excluded.content,
              source=excluded.source,legacy_payload=excluded.legacy_payload,updated_at=excluded.updated_at""",
                (target, account, clean(row.get("content")), clean(row.get("source")) or "external_model",
                 Jsonb(row), parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                 parse_time(row.get("updated_at")) or datetime.now(timezone.utc)))
            self.row_ref(db_name, "external_analysis_notes", row.get("id"), target)

        for row in rows.get("customer_understandings", []):
            account = required_account(row, "customer_understandings")
            if not account:
                continue
            target = self.ref_target(db_name, "customer_understandings", row.get("id"))
            if not target:
                existing = self.cur.execute("select id from trosa.account_understandings where account_id=%s", (account,)).fetchone()
                target = existing[0] if existing else compat_uuid(f"understanding:{account}")
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
                 legacy_int(row.get("version"), 1), parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                 parse_time(row.get("updated_at")) or datetime.now(timezone.utc)))
            actual = self.cur.execute("select id from trosa.account_understandings where account_id=%s", (account,)).fetchone()[0]
            self.row_ref(db_name, "customer_understandings", row.get("id"), actual)

        for row in rows.get("ai_recommendations", []):
            account = required_account(row, "ai_recommendations")
            if not account:
                continue
            target = compat_uuid(f"recommendation:{user}:{row.get('id')}")
            self.execute("""insert into trosa.ai_recommendations
              (id,account_id,understanding_version,content,reason,source_timeline_event_id,review_status,
               user_response,user_modified_content,executed_action,outcome,created_at,updated_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (id) do update set content=excluded.content,reason=excluded.reason,
               source_timeline_event_id=excluded.source_timeline_event_id,review_status=excluded.review_status,
               user_response=excluded.user_response,user_modified_content=excluded.user_modified_content,
               executed_action=excluded.executed_action,outcome=excluded.outcome,updated_at=excluded.updated_at""",
                (target, account, legacy_int(row.get("understanding_version"), 0), clean(row.get("content")), clean(row.get("reason")),
                 self.ref_target(db_name, "follow_up_logs", row.get("source_activity_id")), clean(row.get("review_status")) or "hold",
                 clean(row.get("user_response")), clean(row.get("user_modified_content")), clean(row.get("executed_action")),
                 clean(row.get("outcome")), parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                 parse_time(row.get("updated_at")) or datetime.now(timezone.utc)))
            self.row_ref(db_name, "ai_recommendations", row.get("id"), target)

        for row in rows.get("inbox_items", []):
            account = account_id(row)
            if clean(row.get("customer_id")) and not account:
                self.issue(f"{source_name}/inbox_items", clean(row.get("id")), "MISSING_CUSTOMER", "Inbox row was retained as unassigned because its customer is unavailable", row)
            legacy_inbox_id = self.legacy_id(row.get("id"))
            if legacy_inbox_id is None:
                self.issue(f"{source_name}/inbox_items", clean(row.get("id")), "INVALID_INBOX_ID", "Inbox row has no positive integer id", row)
                continue
            target = compat_uuid(f"inbox:{user}:{legacy_inbox_id}")
            raw_dedupe_key = clean(row.get("dedupe_key"))
            payload = dict(row)
            if raw_dedupe_key:
                payload["compat_dedupe_key"] = raw_dedupe_key
            self.execute("""insert into trosa.inbox_items
              (id,account_id,item_type,title,content,dedupe_key,status,snoozed_until,resolved_at,
               resolution_reason,resolution_note,legacy_payload,created_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (id) do update set account_id=excluded.account_id,item_type=excluded.item_type,
               title=excluded.title,content=excluded.content,status=excluded.status,snoozed_until=excluded.snoozed_until,
               resolved_at=excluded.resolved_at,resolution_reason=excluded.resolution_reason,
               resolution_note=excluded.resolution_note,legacy_payload=excluded.legacy_payload""",
                (target, account, clean(row.get("item_type")), clean(row.get("title")), clean(row.get("content")),
                 compat_dedupe_key(user, raw_dedupe_key), clean(row.get("status")) or "open", parse_time(row.get("snoozed_until")),
                 parse_time(row.get("resolved_at")), clean(row.get("resolution_reason")), clean(row.get("resolution_note")),
                 Jsonb(payload), parse_time(row.get("created_at")) or datetime.now(timezone.utc)))
            self.row_ref(db_name, "inbox_items", legacy_inbox_id, target)

        for row in rows.get("web_monitor_logs", []):
            account = required_account(row, "web_monitor_logs")
            if not account:
                continue
            target = compat_uuid(f"web-monitor:{user}:{row.get('id')}")
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
            account = required_account(row, "customer_files")
            if not account:
                continue
            number = self.legacy_id(row.get("id"))
            if number is None:
                self.issue(f"{source_name}/customer_files", clean(row.get("id")), "INVALID_FILE_ID", "File metadata has no positive integer id", row)
                continue
            file_id = compat_uuid(f"file:{user}:{number}")
            raw_storage_key = clean(row.get("file_path"))
            storage_key = f"{user}:{raw_storage_key}" if raw_storage_key else f"{user}:legacy-file:{number}"
            existing_file = self.cur.execute(
                """select id from core.file_objects
                   where organization_id=%s and storage_key=%s limit 1""",
                (ORG_ID, storage_key),
            ).fetchone()
            if existing_file:
                file_id = existing_file[0]
            self.execute("""insert into core.file_objects
              (id,organization_id,storage_key,original_name,mime_type,size_bytes,sha256,uploaded_by_user_id,deleted_at,created_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (id) do update set storage_key=excluded.storage_key,original_name=excluded.original_name,
              mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,sha256=excluded.sha256,deleted_at=excluded.deleted_at""",
                (file_id, ORG_ID, storage_key, clean(row.get("original_name")), clean(row.get("mime_type")),
                 max(legacy_int(row.get("file_size"), 0), 0), clean(row.get("sha256")), self.identity_user_id(user),
                 parse_time(row.get("deleted_at")) if legacy_bool(row.get("is_deleted")) else None,
                 parse_time(row.get("created_at")) or datetime.now(timezone.utc)))
            self.execute("""insert into core.entity_files(id,file_object_id,account_id,relation_type)
              values (%s,%s,%s,'attachment') on conflict do nothing""", (compat_uuid(f"entity-file:{user}:{number}"), file_id, account))
            self.execute("""insert into trade_os_compat.customer_file_rows
              (legacy_user_id,id,customer_id,account_id,file_object_id,original_name,stored_name,file_path,file_size,mime_type,category,sha256,uploaded_by,is_deleted,deleted_at,created_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (legacy_user_id,id) do update set customer_id=excluded.customer_id,account_id=excluded.account_id,
              file_object_id=excluded.file_object_id,original_name=excluded.original_name,stored_name=excluded.stored_name,
              file_path=excluded.file_path,file_size=excluded.file_size,mime_type=excluded.mime_type,category=excluded.category,
              sha256=excluded.sha256,uploaded_by=excluded.uploaded_by,is_deleted=excluded.is_deleted,deleted_at=excluded.deleted_at""",
                (user, number, self.legacy_id(row.get("customer_id")), account, file_id, clean(row.get("original_name")),
                 clean(row.get("stored_name")), raw_storage_key, max(legacy_int(row.get("file_size"), 0), 0), clean(row.get("mime_type")),
                 clean(row.get("category")), clean(row.get("sha256")), clean(row.get("uploaded_by")), int(legacy_bool(row.get("is_deleted"))),
                 clean(row.get("deleted_at")), clean(row.get("created_at"))))

        self.import_trosa_runtime_rows(db_name, rows)

    def import_trosa_runtime_rows(self, db_name: str, rows: dict[str, list[dict[str, Any]]]) -> None:
        """Preserve user-scoped audit/integration ledgers in PostgreSQL runtime tables."""
        user = legacy_user_key(db_name)

        def next_id(table: str) -> int:
            if table in {'email_verifications', 'email_verification_jobs', 'email_domain_probes'}:
                source_table = {
                    'email_verifications': 'email_verifications',
                    'email_verification_jobs': 'email_verification_jobs',
                    'email_domain_probes': 'email_domain_probes',
                }[table]
                row = self.cur.execute(
                    f"select coalesce(max(legacy_id),0)+1 from trosa.{source_table} where organization_id=%s and legacy_user_id=%s",
                    (ORG_ID, user),
                ).fetchone()
            elif table == 'email_logs':
                row = self.cur.execute(
                    """select coalesce(max(case when legacy_key ~ '^[0-9]+$'
                                      then legacy_key::bigint else 0 end),0)+1
                         from trosa.email_logs
                        where organization_id=%s and legacy_user_id=%s""",
                    (ORG_ID, user),
                ).fetchone()
            else:
                source_table = {
                    'integration_sync_receipts': 'integration_sync_receipt_rows',
                    'agent_gateway_idempotency': 'agent_gateway_rows',
                }.get(table, table)
                row = self.cur.execute(f"select coalesce(max(id),0)+1 from trade_os_compat.{source_table} where legacy_user_id=%s", (user,)).fetchone()
            return int(row[0])

        def email_legacy_id(
            table: str,
            natural_expression: str,
            natural_value: str,
            requested: int | None,
            source_path: str,
        ) -> int:
            """Resolve a legacy id without violating either unique key.

            These current-state tables have both a natural key (email/domain)
            and a compatibility key (legacy_id).  A historical export can
            contain duplicate rows with different ids, or the target can
            already contain a row whose legacy id was never backfilled.  Pick
            the existing natural row first, then allocate a collision-free
            compatibility id and record the ambiguity instead of aborting the
            whole migration.
            """
            existing = self.cur.execute(
                f"""select id, legacy_id from trosa.{table}
                    where organization_id=%s and legacy_user_id=%s
                      and {natural_expression}=%s
                    limit 1""",
                (ORG_ID, user, natural_value),
            ).fetchone()
            candidate = requested or next_id(table)
            if existing:
                existing_legacy = self.legacy_id(existing[1])
                if existing_legacy is not None:
                    if requested is not None and requested != existing_legacy:
                        self.issue(
                            source_path,
                            str(requested),
                            'DUPLICATE_EMAIL_NATURAL_KEY',
                            'The natural email/domain key already existed; its existing compatibility id was retained',
                        )
                    return existing_legacy
                conflict = self.cur.execute(
                    f"""select id from trosa.{table}
                        where organization_id=%s and legacy_user_id=%s
                          and legacy_id=%s and id<>%s
                        limit 1""",
                    (ORG_ID, user, candidate, existing[0]),
                ).fetchone()
                if conflict:
                    self.issue(
                        source_path,
                        str(candidate),
                        'EMAIL_LEGACY_ID_COLLISION',
                        'The requested legacy id belonged to another natural key; a new compatibility id was allocated',
                    )
                    candidate = next_id(table)
                self.execute(
                    f"update trosa.{table} set legacy_id=%s where organization_id=%s and legacy_user_id=%s and id=%s",
                    (candidate, ORG_ID, user, existing[0]),
                )
                return candidate

            conflict = self.cur.execute(
                f"""select id from trosa.{table}
                    where organization_id=%s and legacy_user_id=%s and legacy_id=%s
                    limit 1""",
                (ORG_ID, user, candidate),
            ).fetchone()
            if conflict:
                self.issue(
                    source_path,
                    str(candidate),
                    'EMAIL_LEGACY_ID_COLLISION',
                    'The requested legacy id belonged to another natural key; a new compatibility id was allocated',
                )
                candidate = next_id(table)
            return candidate

        def canonical_batch_id(value: Any, source_path: str) -> uuid.UUID:
            """Resolve a legacy batch reference without inventing a FK."""
            legacy_batch_id = self.legacy_id(value)
            if legacy_batch_id is None:
                return self.batch_id
            candidate = compat_uuid(f"import-batch:{user}:{legacy_batch_id}")
            if self.cur.execute(
                "select 1 from audit.import_batches where organization_id=%s and id=%s",
                (ORG_ID, candidate),
            ).fetchone():
                return candidate
            self.issue(
                source_path,
                str(legacy_batch_id),
                "MISSING_IMPORT_BATCH",
                "The legacy batch reference was not projected; the current source batch was used so the audit row remains valid",
            )
            return self.batch_id

        for row in rows.get("integration_sync_receipts", []):
            number = self.legacy_id(row.get("id"))
            if number is None:
                number = next_id("integration_sync_receipts")
            integration = clean(row.get("integration")) or "legacy_import"
            idempotency_key = clean(row.get("idempotency_key"))
            if not clean(row.get("integration")) or not idempotency_key:
                self.issue(
                    f"trosa/{db_name}/integration_sync_receipts",
                    str(number),
                    "INVALID_RECEIPT_KEY",
                    "Receipt was missing integration or idempotency_key; a stable legacy fallback was used",
                    row,
                )
            if not idempotency_key:
                idempotency_key = f"legacy:{number}"
            existing_key = self.cur.execute(
                """select id from trade_os_compat.integration_sync_receipt_rows
                   where legacy_user_id=%s and integration=%s and idempotency_key=%s
                   limit 1""",
                (user, integration, idempotency_key),
            ).fetchone()
            if existing_key:
                if existing_key[0] != number:
                    self.issue(
                        f"trosa/{db_name}/integration_sync_receipts",
                        str(number),
                        "DUPLICATE_RECEIPT_KEY",
                        "Duplicate idempotency key reused the existing receipt row",
                        row,
                    )
                number = existing_key[0]
            else:
                existing_id = self.cur.execute(
                    """select integration,idempotency_key
                       from trade_os_compat.integration_sync_receipt_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and tuple(existing_id) != (integration, idempotency_key):
                    self.issue(
                        f"trosa/{db_name}/integration_sync_receipts",
                        str(number),
                        "RECEIPT_ID_COLLISION",
                        "Receipt id already belongs to another key; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("integration_sync_receipts")
            response_json = clean(row.get("response_json")) or "{}"
            self.execute(
                """insert into trade_os_compat.integration_sync_receipt_rows
                   (legacy_user_id,id,integration,idempotency_key,request_sha256,
                    candidate_id,customer_id,response_json,created_at,updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (legacy_user_id,id) do update set
                    integration=excluded.integration,idempotency_key=excluded.idempotency_key,
                    request_sha256=excluded.request_sha256,candidate_id=excluded.candidate_id,
                    customer_id=excluded.customer_id,response_json=excluded.response_json,
                    created_at=excluded.created_at,updated_at=excluded.updated_at""",
                (
                    user,
                    number,
                    integration,
                    idempotency_key,
                    clean(row.get("request_sha256")) or f"legacy:{number}",
                    clean(row.get("candidate_id")),
                    self.legacy_id(row.get("customer_id")),
                    response_json,
                    clean(row.get("created_at")),
                    clean(row.get("updated_at")),
                ),
            )
            response_payload = json_value(response_json)
            if not isinstance(response_payload, dict):
                response_payload = {"legacy_response_json": response_json}
            else:
                response_payload = dict(response_payload)
                response_payload["_legacy_user_id"] = user
                response_payload["_legacy_idempotency_key"] = idempotency_key
            account = self.account_for(db_name, row.get("customer_id"))
            created_at = parse_time(row.get("created_at")) or datetime.now(timezone.utc)
            updated_at = parse_time(row.get("updated_at")) or created_at
            self.execute(
                """insert into audit.integration_receipts
                   (id,organization_id,integration,idempotency_key,request_sha256,
                    legacy_candidate_id,account_id,response_payload,created_at,updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (id) do update set
                    integration=excluded.integration,idempotency_key=excluded.idempotency_key,
                    request_sha256=excluded.request_sha256,legacy_candidate_id=excluded.legacy_candidate_id,
                    account_id=excluded.account_id,response_payload=excluded.response_payload,
                    updated_at=excluded.updated_at""",
                (
                    compat_uuid(f"integration-receipt:{user}:{integration}:{idempotency_key}"),
                    ORG_ID,
                    integration,
                    f"{user}:{idempotency_key}",
                    clean(row.get("request_sha256")) or f"legacy:{number}",
                    clean(row.get("candidate_id")),
                    account,
                    Jsonb(response_payload),
                    created_at,
                    updated_at,
                ),
            )

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
            number = self.legacy_id(row.get("id")) or next_id("agent_proposal_rows")
            customer_id = self.legacy_id(row.get("customer_id"))
            self.execute(
                """insert into trade_os_compat.agent_proposal_rows
                   (legacy_user_id,id,proposal_type,customer_id,payload,proposal_action,
                    source,source_reference,idempotency_key,request_sha256,status,created_at,confirmed_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(legacy_user_id,id) do update set
                    proposal_type=excluded.proposal_type,customer_id=excluded.customer_id,
                    payload=excluded.payload,proposal_action=excluded.proposal_action,
                    source=excluded.source,source_reference=excluded.source_reference,
                    idempotency_key=excluded.idempotency_key,request_sha256=excluded.request_sha256,
                    status=excluded.status,created_at=excluded.created_at,confirmed_at=excluded.confirmed_at""",
                (
                    user,
                    number,
                    clean(row.get("proposal_type")),
                    customer_id or 0,
                    clean(row.get("payload")) or "{}",
                    clean(row.get("proposal_action")),
                    clean(row.get("source")),
                    clean(row.get("source_reference")),
                    clean(row.get("idempotency_key")),
                    clean(row.get("request_sha256")),
                    clean(row.get("status")) or "pending",
                    clean(row.get("created_at")),
                    clean(row.get("confirmed_at")),
                ),
            )
            account = self.account_for(db_name, customer_id)
            if account is None:
                self.issue(
                    f"trosa/{db_name}/agent_proposals",
                    str(number),
                    "MISSING_PROPOSAL_CUSTOMER",
                    "Proposal was retained in the compatibility ledger, but its canonical audit projection was skipped because the customer is missing or invalid",
                    row,
                )
                continue
            self.execute(
                """insert into audit.agent_proposals
                   (id,organization_id,account_id,proposal_type,payload,proposal_action,
                    source,source_reference,idempotency_key,request_sha256,status,created_at,confirmed_at)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(id) do update set
                    account_id=excluded.account_id,proposal_type=excluded.proposal_type,
                    payload=excluded.payload,proposal_action=excluded.proposal_action,
                    source=excluded.source,source_reference=excluded.source_reference,
                    idempotency_key=excluded.idempotency_key,request_sha256=excluded.request_sha256,
                    status=excluded.status,created_at=excluded.created_at,confirmed_at=excluded.confirmed_at""",
                (
                    compat_uuid(f"agent-proposal:{user}:{number}"),
                    ORG_ID,
                    account,
                    clean(row.get("proposal_type")),
                    Jsonb(json_value(row.get("payload")) or {}),
                    clean(row.get("proposal_action")),
                    clean(row.get("source")),
                    clean(row.get("source_reference")),
                    clean(row.get("idempotency_key")),
                    clean(row.get("request_sha256")),
                    clean(row.get("status")) or "pending",
                    parse_time(row.get("created_at")) or datetime.now(timezone.utc),
                    parse_time(row.get("confirmed_at")),
                ),
            )

        for row in rows.get("agent_gateway_idempotency", []):
            number = self.legacy_id(row.get("id"))
            action = clean(row.get("action"))
            idempotency_key = clean(row.get("idempotency_key"))
            if not action or not idempotency_key:
                number = number or next_id("agent_gateway_idempotency")
                self.issue(
                    f"trosa/{db_name}/agent_gateway_idempotency",
                    str(number),
                    "INVALID_GATEWAY_KEY",
                    "Gateway row was missing action or idempotency_key; a stable legacy fallback was used",
                    row,
                )
                action = action or "legacy_import"
                idempotency_key = idempotency_key or f"legacy:{number}"

            existing_key = self.cur.execute(
                """select id from trade_os_compat.agent_gateway_rows
                   where legacy_user_id=%s and action=%s and idempotency_key=%s
                   limit 1""",
                (user, action, idempotency_key),
            ).fetchone()
            if existing_key:
                if number is not None and existing_key[0] != number:
                    self.issue(
                        f"trosa/{db_name}/agent_gateway_idempotency",
                        str(number),
                        "DUPLICATE_GATEWAY_KEY",
                        "Duplicate gateway idempotency key reused the existing compatibility row",
                        row,
                    )
                number = existing_key[0]
            else:
                number = number or next_id("agent_gateway_idempotency")
                existing_id = self.cur.execute(
                    """select action,idempotency_key
                       from trade_os_compat.agent_gateway_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and tuple(existing_id) != (action, idempotency_key):
                    self.issue(
                        f"trosa/{db_name}/agent_gateway_idempotency",
                        str(number),
                        "GATEWAY_ID_COLLISION",
                        "Gateway row id is already used by another idempotency key; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("agent_gateway_idempotency")

            response_json = clean(row.get("response_json")) or "{}"
            response_payload = json_value(response_json)
            if not isinstance(response_payload, (dict, list)):
                response_payload = {"legacy_response_json": response_json}
            proposal_id = self.legacy_id(row.get("proposal_id"))
            proposal_uuid = None
            if proposal_id is not None:
                candidate_proposal = compat_uuid(f"agent-proposal:{user}:{proposal_id}")
                if self.cur.execute(
                    "select 1 from audit.agent_proposals where organization_id=%s and id=%s",
                    (ORG_ID, candidate_proposal),
                ).fetchone():
                    proposal_uuid = candidate_proposal
                else:
                    self.issue(
                        f"trosa/{db_name}/agent_gateway_idempotency",
                        str(number),
                        "MISSING_GATEWAY_PROPOSAL",
                        "Gateway row references a proposal that was not projected; proposal_id was cleared in the canonical audit row",
                        row,
                    )
            created_at = parse_time(row.get("created_at")) or datetime.now(timezone.utc)
            updated_at = parse_time(row.get("updated_at")) or created_at
            request_sha256 = clean(row.get("request_sha256")) or f"legacy:{number}"
            self.execute(
                """insert into trade_os_compat.agent_gateway_rows
                   (legacy_user_id,id,action,idempotency_key,request_sha256,proposal_id,
                    response_json,created_at,updated_at)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(legacy_user_id,id) do update set
                    action=excluded.action,idempotency_key=excluded.idempotency_key,
                    request_sha256=excluded.request_sha256,proposal_id=excluded.proposal_id,
                    response_json=excluded.response_json,created_at=excluded.created_at,
                    updated_at=excluded.updated_at""",
                (
                    user,
                    number,
                    action,
                    idempotency_key,
                    request_sha256,
                    proposal_id,
                    response_json,
                    created_at.isoformat(),
                    updated_at.isoformat(),
                ),
            )
            self.execute(
                """insert into audit.agent_gateway_idempotency
                   (id,organization_id,legacy_user_id,action,idempotency_key,request_sha256,
                    proposal_id,response_json,created_at,updated_at)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (organization_id,legacy_user_id,action,idempotency_key) do update set
                    request_sha256=excluded.request_sha256,proposal_id=excluded.proposal_id,
                    response_json=excluded.response_json,updated_at=excluded.updated_at""",
                (
                    compat_uuid(f"agent-gateway:{user}:{action}:{idempotency_key}"),
                    ORG_ID,
                    user,
                    action,
                    idempotency_key,
                    request_sha256,
                    proposal_uuid,
                    Jsonb(response_payload),
                    created_at,
                    updated_at,
                ),
            )

        for row in rows.get("agent_actions", []):
            raw_action_id = clean(row.get("action_id"))
            number = self.legacy_id(row.get("id"))
            if not raw_action_id:
                number = number or next_id("agent_action_rows")
                action_id = f"legacy:{user}:{number}"
            else:
                action_id = raw_action_id
            existing_action = self.cur.execute(
                """select id from trade_os_compat.agent_action_rows
                   where legacy_user_id=%s and action_id=%s limit 1""",
                (user, action_id),
            ).fetchone()
            if existing_action:
                if number is not None and existing_action[0] != number:
                    self.issue(
                        f"trosa/{db_name}/agent_actions",
                        str(number),
                        "DUPLICATE_AGENT_ACTION",
                        "Duplicate action_id reused the existing compatibility row",
                        row,
                    )
                number = existing_action[0]
            else:
                number = number or next_id("agent_action_rows")
                existing_id = self.cur.execute(
                    """select action_id from trade_os_compat.agent_action_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and existing_id[0] != action_id:
                    self.issue(
                        f"trosa/{db_name}/agent_actions",
                        str(number),
                        "AGENT_ACTION_ID_COLLISION",
                        "Action id is already used by another action; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("agent_action_rows")
                    if not raw_action_id:
                        action_id = f"legacy:{user}:{number}"
            undo_token = clean(row.get("undo_token")) or f"legacy:{user}:{number}"
            self.execute("""insert into trade_os_compat.agent_action_rows(legacy_user_id,id,action_id,token_id,user_id,action_type,customer_id,related_type,related_id,undo_token,request_json,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              action_id=excluded.action_id,token_id=excluded.token_id,user_id=excluded.user_id,action_type=excluded.action_type,
              customer_id=excluded.customer_id,related_type=excluded.related_type,related_id=excluded.related_id,
              undo_token=excluded.undo_token,request_json=excluded.request_json,status=excluded.status,
              created_at=excluded.created_at,undone_at=excluded.undone_at""",
              (user,number,action_id,clean(row.get("token_id")),clean(row.get("user_id")) or user,clean(row.get("action_type")),self.legacy_id(row.get("customer_id")),clean(row.get("related_type")),self.legacy_id(row.get("related_id")),undo_token,clean(row.get("request_json")) or "{}",clean(row.get("status")) or "completed",clean(row.get("created_at")),clean(row.get("undone_at"))))
            account=self.account_for(db_name,row.get("customer_id"))
            actor=self.cur.execute("select id from identity.users where organization_id=%s and legacy_user_id=%s",(ORG_ID,user)).fetchone()
            self.execute("""insert into audit.agent_actions(id,organization_id,legacy_user_id,action_id,token_id,actor_user_id,action_type,account_id,related_type,related_id,undo_token,request_payload,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(organization_id,legacy_user_id,action_id) do update set
              token_id=excluded.token_id,actor_user_id=excluded.actor_user_id,action_type=excluded.action_type,
              account_id=excluded.account_id,related_type=excluded.related_type,related_id=excluded.related_id,
              undo_token=excluded.undo_token,request_payload=excluded.request_payload,status=excluded.status,
              created_at=excluded.created_at,undone_at=excluded.undone_at""",
              (compat_uuid(f"agent-action:{user}:{action_id}"),ORG_ID,user,action_id,clean(row.get("token_id")),actor[0] if actor else None,clean(row.get("action_type")),account,clean(row.get("related_type")),clean(row.get("related_id")),undo_token,Jsonb(json_value(row.get("request_json")) or {}),clean(row.get("status")) or "completed",parse_time(row.get("created_at")) or datetime.now(timezone.utc),parse_time(row.get("undone_at"))))

        for row in rows.get("undo_actions", []):
            raw_token = clean(row.get("token"))
            number = self.legacy_id(row.get("id"))
            if not raw_token:
                number = number or next_id("undo_action_rows")
                token = f"legacy:{user}:{number}"
            else:
                token = raw_token
            existing_token = self.cur.execute(
                """select id from trade_os_compat.undo_action_rows
                   where legacy_user_id=%s and token=%s limit 1""",
                (user, token),
            ).fetchone()
            if existing_token:
                if number is not None and existing_token[0] != number:
                    self.issue(
                        f"trosa/{db_name}/undo_actions",
                        str(number),
                        "DUPLICATE_UNDO_TOKEN",
                        "Duplicate undo token reused the existing compatibility row",
                        row,
                    )
                number = existing_token[0]
            else:
                number = number or next_id("undo_action_rows")
                existing_id = self.cur.execute(
                    """select token from trade_os_compat.undo_action_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and existing_id[0] != token:
                    self.issue(
                        f"trosa/{db_name}/undo_actions",
                        str(number),
                        "UNDO_ACTION_ID_COLLISION",
                        "Undo action id is already used by another token; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("undo_action_rows")
                    if not raw_token:
                        token = f"legacy:{user}:{number}"
            entities=json_value(row.get("entities")) or []
            self.execute("""insert into trade_os_compat.undo_action_rows(legacy_user_id,id,token,operation,target_type,target_id,description,entities,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              token=excluded.token,operation=excluded.operation,target_type=excluded.target_type,target_id=excluded.target_id,
              description=excluded.description,entities=excluded.entities,status=excluded.status,
              created_at=excluded.created_at,undone_at=excluded.undone_at""",
              (user,number,token,clean(row.get("operation")),clean(row.get("target_type")),self.legacy_id(row.get("target_id")),clean(row.get("description")),json.dumps(entities,ensure_ascii=False),clean(row.get("status")) or "available",clean(row.get("created_at")),clean(row.get("undone_at"))))
            self.execute("""insert into audit.undo_snapshots(id,organization_id,legacy_user_id,token,operation,target_type,target_id,description,entities,status,created_at,undone_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(organization_id,legacy_user_id,token) do update set
              operation=excluded.operation,target_type=excluded.target_type,target_id=excluded.target_id,
              description=excluded.description,entities=excluded.entities,status=excluded.status,
              created_at=excluded.created_at,undone_at=excluded.undone_at""",
              (compat_uuid(f"undo:{user}:{token}"),ORG_ID,user,token,clean(row.get("operation")),clean(row.get("target_type")),clean(row.get("target_id")),clean(row.get("description")),Jsonb(entities),clean(row.get("status")) or "available",parse_time(row.get("created_at")) or datetime.now(timezone.utc),parse_time(row.get("undone_at"))))

        for row in rows.get("email_verifications", []):
            email = clean(row.get("email")).lower()
            normalized_email = (clean(row.get("normalized_email")) or email).lower()
            if not normalized_email:
                self.issue(
                    f"trosa/{db_name}/email_verifications",
                    clean(row.get("id")),
                    'INVALID_EMAIL_KEY',
                    'Email verification was kept in the audit archive because its natural email key is empty',
                    row,
                )
                continue
            number = email_legacy_id(
                'email_verifications', 'lower(normalized_email)', normalized_email,
                self.legacy_id(row.get("id")), f"trosa/{db_name}/email_verifications",
            )
            self.execute("""insert into trosa.email_verifications(organization_id,legacy_user_id,legacy_id,email,normalized_email,domain,deliverability_status,confidence,address_type,risk_flags,evidence,mx_records,checked_at,expires_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,legacy_user_id,legacy_id) where legacy_id is not null do update set email=excluded.email,normalized_email=excluded.normalized_email,domain=excluded.domain,deliverability_status=excluded.deliverability_status,confidence=excluded.confidence,risk_flags=excluded.risk_flags,evidence=excluded.evidence,mx_records=excluded.mx_records,checked_at=excluded.checked_at,expires_at=excluded.expires_at""",
              (ORG_ID,user,number,email,normalized_email,clean(row.get("domain")).lower(),clean(row.get("deliverability_status")) or "unknown",clean(row.get("confidence")) or "low",clean(row.get("address_type")) or "person",Jsonb(json_value(row.get("risk_flags")) or []),Jsonb(json_value(row.get("evidence")) or []),Jsonb(json_value(row.get("mx_records")) or []),clean(row.get("checked_at")),clean(row.get("expires_at"))))
        for row in rows.get("email_verification_jobs", []):
            email = clean(row.get("email")).lower()
            if not email:
                self.issue(
                    f"trosa/{db_name}/email_verification_jobs",
                    clean(row.get("id")),
                    'INVALID_EMAIL_KEY',
                    'Email verification job was kept in the audit archive because its natural email key is empty',
                    row,
                )
                continue
            number = email_legacy_id(
                'email_verification_jobs', 'lower(email)', email,
                self.legacy_id(row.get("id")), f"trosa/{db_name}/email_verification_jobs",
            )
            self.execute("""insert into trosa.email_verification_jobs(organization_id,legacy_user_id,legacy_id,email,domain,status,attempts,next_run_at,last_error,created_at,updated_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,legacy_user_id,legacy_id) where legacy_id is not null do update set email=excluded.email,domain=excluded.domain,status=excluded.status,attempts=excluded.attempts,next_run_at=excluded.next_run_at,last_error=excluded.last_error,updated_at=excluded.updated_at""",
              (ORG_ID,user,number,email,clean(row.get("domain")).lower(),clean(row.get("status")) or "queued",max(legacy_int(row.get("attempts"), 0), 0),clean(row.get("next_run_at")),clean(row.get("last_error")),clean(row.get("created_at")),clean(row.get("updated_at"))))
        for row in rows.get("email_domain_probes", []):
            probe_domain = clean(row.get("domain")).lower()
            if not probe_domain:
                self.issue(
                    f"trosa/{db_name}/email_domain_probes",
                    clean(row.get("id")),
                    'INVALID_DOMAIN_KEY',
                    'Domain probe was kept in the audit archive because its natural domain key is empty',
                    row,
                )
                continue
            number = email_legacy_id(
                'email_domain_probes', 'lower(domain)', probe_domain,
                self.legacy_id(row.get("id")), f"trosa/{db_name}/email_domain_probes",
            )
            self.execute("""insert into trosa.email_domain_probes(organization_id,legacy_user_id,legacy_id,domain,catchall_status,evidence,checked_at,next_check_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,legacy_user_id,legacy_id) where legacy_id is not null do update set domain=excluded.domain,catchall_status=excluded.catchall_status,evidence=excluded.evidence,checked_at=excluded.checked_at,next_check_at=excluded.next_check_at""",
              (ORG_ID,user,number,probe_domain,clean(row.get("catchall_status")) or "unknown",Jsonb(json_value(row.get("evidence")) or []),clean(row.get("checked_at")),clean(row.get("next_check_at"))))
        for row in rows.get("email_logs", []):
            number=self.legacy_id(row.get("id")) or next_id("email_logs")
            self.execute("""insert into trosa.email_logs(organization_id,legacy_user_id,legacy_key,status,message,reminder_count,created_at)
              values(%s,%s,%s,%s,%s,%s,%s) on conflict(organization_id,legacy_user_id,legacy_key) where legacy_key <> '' do update set status=excluded.status,message=excluded.message,reminder_count=excluded.reminder_count,created_at=excluded.created_at""",
              (ORG_ID,user,str(number),clean(row.get("status")),clean(row.get("message")),max(legacy_int(row.get("reminder_count"), 0), 0),clean(row.get("created_at"))))

        for row in rows.get("import_batches", []):
            source_name = clean(row.get("source_name"))
            source_sha256 = clean(row.get("source_sha256"))
            number = self.legacy_id(row.get("id"))
            existing_natural = self.cur.execute(
                """select id from trade_os_compat.import_batch_rows
                   where legacy_user_id=%s and source_name=%s and source_sha256=%s
                   limit 1""",
                (user, source_name, source_sha256),
            ).fetchone()
            if existing_natural:
                number = existing_natural[0]
            else:
                number = number or next_id("import_batch_rows")
                existing_id = self.cur.execute(
                    """select source_name,source_sha256
                       from trade_os_compat.import_batch_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and tuple(existing_id) != (source_name, source_sha256):
                    self.issue(
                        f"trosa/{db_name}/import_batches",
                        str(number),
                        "IMPORT_BATCH_ID_COLLISION",
                        "Legacy import batch id is already used by another source; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("import_batch_rows")
            imported_count = max(legacy_int(row.get("imported_count"), 0) or 0, 0)
            skipped_count = max(legacy_int(row.get("skipped_count"), 0) or 0, 0)
            created_customers = max(legacy_int(row.get("created_customers"), 0) or 0, 0)
            imported_at = parse_time(row.get("imported_at")) or datetime.now(timezone.utc)
            self.execute(
                """insert into trade_os_compat.import_batch_rows
                   (legacy_user_id,id,source_name,source_sha256,imported_at,imported_count,
                    skipped_count,created_customers,details)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(legacy_user_id,id) do update set
                    source_name=excluded.source_name,source_sha256=excluded.source_sha256,
                    imported_at=excluded.imported_at,imported_count=excluded.imported_count,
                    skipped_count=excluded.skipped_count,created_customers=excluded.created_customers,
                    details=excluded.details""",
                (
                    user,
                    number,
                    source_name,
                    source_sha256,
                    imported_at.isoformat(),
                    imported_count,
                    skipped_count,
                    created_customers,
                    clean(row.get("details")),
                ),
            )
            self.execute(
                """insert into audit.import_batches
                   (id,organization_id,source_name,source_path,source_sha256,source_rows,imported_at)
                   values(%s,%s,%s,%s,%s,%s,%s)
                   on conflict(id) do update set
                    source_name=excluded.source_name,source_path=excluded.source_path,
                    source_sha256=excluded.source_sha256,source_rows=excluded.source_rows,
                    imported_at=excluded.imported_at""",
                (
                    compat_uuid(f"import-batch:{user}:{number}"),
                    ORG_ID,
                    source_name,
                    f"compat/{user}/{source_name}/{number}",
                    source_sha256,
                    imported_count + skipped_count,
                    imported_at,
                ),
            )

        for row in rows.get("imported_activity_rows", []):
            number=self.legacy_id(row.get("id")) or next_id("imported_activity_row_rows")
            raw_activity_hash = clean(row.get("activity_hash"))
            activity_hash = raw_activity_hash or str(
                compat_uuid(f"import-activity:{user}:{number}")
            )
            audit_activity_hash = f"{user}:{activity_hash}"
            batch_id = canonical_batch_id(
                row.get("batch_id"), f"trosa/{db_name}/imported_activity_rows"
            )
            self.execute("""insert into trade_os_compat.imported_activity_row_rows
              (legacy_user_id,id,activity_hash,source_key,batch_id,customer_id,source_name,source_sheet,source_cell,source_header,activity_id,imported_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              activity_hash=excluded.activity_hash,source_key=excluded.source_key,batch_id=excluded.batch_id,customer_id=excluded.customer_id,
              source_name=excluded.source_name,source_sheet=excluded.source_sheet,source_cell=excluded.source_cell,source_header=excluded.source_header,
              activity_id=excluded.activity_id,imported_at=excluded.imported_at""",
              (user,number,activity_hash,clean(row.get("source_key")),self.legacy_id(row.get("batch_id")),self.legacy_id(row.get("customer_id")) or 0,clean(row.get("source_name")),clean(row.get("source_sheet")),clean(row.get("source_cell")),clean(row.get("source_header")),self.legacy_id(row.get("activity_id")),clean(row.get("imported_at"))))
            account=self.account_for(db_name,row.get("customer_id")); activity=self.ref_target(db_name,"follow_up_logs",row.get("activity_id"))
            if account:
                self.execute("""insert into audit.imported_activity_rows
                  (id,organization_id,legacy_user_id,activity_hash,source_key,batch_id,account_id,source_name,source_sheet,source_cell,source_header,activity_id)
                  values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict(organization_id,activity_hash) do update set source_key=excluded.source_key,account_id=excluded.account_id,activity_id=excluded.activity_id""",
                  (compat_uuid(f"imported-activity:{user}:{activity_hash}"),ORG_ID,user,audit_activity_hash,clean(row.get("source_key")),batch_id,account,clean(row.get("source_name")),clean(row.get("source_sheet")),clean(row.get("source_cell")),clean(row.get("source_header")),activity))

        for row in rows.get("import_unmatched_customers", []):
            number=self.legacy_id(row.get("id")) or next_id("import_unmatched_customer_rows")
            raw_unmatched_hash = clean(row.get("unmatched_hash"))
            unmatched_hash = raw_unmatched_hash or str(
                compat_uuid(f"unmatched:{user}:{number}")
            )
            audit_unmatched_hash = f"{user}:{unmatched_hash}"
            batch_id = canonical_batch_id(
                row.get("batch_id"), f"trosa/{db_name}/import_unmatched_customers"
            )
            self.execute("""insert into trade_os_compat.import_unmatched_customer_rows
              (legacy_user_id,id,unmatched_hash,batch_id,customer_name,country,website,source_sheet,source_row,reason,created_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              unmatched_hash=excluded.unmatched_hash,batch_id=excluded.batch_id,customer_name=excluded.customer_name,country=excluded.country,
              website=excluded.website,source_sheet=excluded.source_sheet,source_row=excluded.source_row,reason=excluded.reason,created_at=excluded.created_at""",
              (user,number,unmatched_hash,self.legacy_id(row.get("batch_id")),clean(row.get("customer_name")),clean(row.get("country")),clean(row.get("website")),clean(row.get("source_sheet")),self.legacy_id(row.get("source_row")),clean(row.get("reason")),clean(row.get("created_at"))))
            self.execute("""insert into audit.import_unmatched_customers
              (id,organization_id,legacy_user_id,unmatched_hash,batch_id,customer_name,country,website,source_sheet,source_row,reason)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict(organization_id,unmatched_hash) do update set customer_name=excluded.customer_name,reason=excluded.reason""",
              (compat_uuid(f"unmatched:{user}:{unmatched_hash}"),ORG_ID,user,audit_unmatched_hash,batch_id,clean(row.get("customer_name")),clean(row.get("country")),clean(row.get("website")),clean(row.get("source_sheet")),self.legacy_id(row.get("source_row")),clean(row.get("reason"))))

        for row in rows.get("gmail_message_states", []):
            number = self.legacy_id(row.get("id"))
            provider = clean(row.get("provider_message_id"))
            has_provider = bool(provider)
            if not provider:
                number = number or next_id("gmail_message_state_rows")
                self.issue(
                    f"trosa/{db_name}/gmail_message_states",
                    str(number),
                    "MISSING_PROVIDER_MESSAGE_ID",
                    "Gmail state had no provider message id; a stable legacy fallback was used",
                    row,
                )
                provider = f"legacy:{user}:{number}"
            existing_provider = self.cur.execute(
                """select id from trade_os_compat.gmail_message_state_rows
                   where legacy_user_id=%s and provider_message_id=%s limit 1""",
                (user, provider),
            ).fetchone()
            if existing_provider:
                if number is not None and existing_provider[0] != number:
                    self.issue(
                        f"trosa/{db_name}/gmail_message_states",
                        str(number),
                        "DUPLICATE_PROVIDER_MESSAGE_ID",
                        "Duplicate provider message id reused the existing compatibility row",
                        row,
                    )
                number = existing_provider[0]
            else:
                number = number or next_id("gmail_message_state_rows")
                existing_id = self.cur.execute(
                    """select provider_message_id from trade_os_compat.gmail_message_state_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and existing_id[0] != provider:
                    self.issue(
                        f"trosa/{db_name}/gmail_message_states",
                        str(number),
                        "GMAIL_STATE_ID_COLLISION",
                        "Gmail state id is already used by another provider message; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("gmail_message_state_rows")
                    if not has_provider:
                        provider = f"legacy:{user}:{number}"

            recipient_raw = row.get("recipient_emails")
            recipient_payload = json_value(recipient_raw)
            if recipient_payload is None or recipient_payload == "":
                recipient_payload = []
            elif not isinstance(recipient_payload, list):
                recipient_payload = [recipient_payload]
            raw_payload = json_value(row.get("raw_payload"))
            if not isinstance(raw_payload, (dict, list)):
                raw_payload = {"legacy_raw_payload": clean(row.get("raw_payload"))}
            customer_id = self.legacy_id(row.get("customer_id"))
            contact_id = self.legacy_id(row.get("contact_id"))
            activity_id = self.legacy_id(row.get("activity_id"))
            inbox_item_id = self.legacy_id(row.get("inbox_item_id"))
            account = self.account_for(db_name, customer_id)
            contact = self.contact_method_for(db_name, contact_id, row.get("sender_email"))
            timeline = self.ref_target(db_name, "follow_up_logs", activity_id)
            inbox_item = self.ref_target(db_name, "inbox_items", inbox_item_id)
            created_at = parse_time(row.get("created_at")) or datetime.now(timezone.utc)
            updated_at = parse_time(row.get("updated_at")) or created_at
            recipient_text = (
                clean(recipient_raw)
                if not isinstance(recipient_raw, (dict, list))
                else json.dumps(recipient_raw, ensure_ascii=False)
            ) or "[]"
            raw_text = (
                clean(row.get("raw_payload"))
                if not isinstance(row.get("raw_payload"), (dict, list))
                else json.dumps(row.get("raw_payload"), ensure_ascii=False)
            ) or "{}"
            match_status = clean(row.get("match_status")) or "unmatched"
            self.execute(
                """insert into trade_os_compat.gmail_message_state_rows
                   (legacy_user_id,id,provider_message_id,provider_thread_id,message_time,
                    sender_email,recipient_emails,subject,customer_id,contact_id,match_status,
                    activity_id,inbox_item_id,raw_payload,last_error,created_at,updated_at)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(legacy_user_id,id) do update set
                    provider_message_id=excluded.provider_message_id,
                    provider_thread_id=excluded.provider_thread_id,message_time=excluded.message_time,
                    sender_email=excluded.sender_email,recipient_emails=excluded.recipient_emails,
                    subject=excluded.subject,customer_id=excluded.customer_id,contact_id=excluded.contact_id,
                    match_status=excluded.match_status,activity_id=excluded.activity_id,
                    inbox_item_id=excluded.inbox_item_id,raw_payload=excluded.raw_payload,
                    last_error=excluded.last_error,created_at=excluded.created_at,updated_at=excluded.updated_at""",
                (
                    user,
                    number,
                    provider,
                    clean(row.get("provider_thread_id")),
                    clean(row.get("message_time")),
                    clean(row.get("sender_email")),
                    recipient_text,
                    clean(row.get("subject")),
                    customer_id,
                    contact_id,
                    match_status,
                    activity_id,
                    inbox_item_id,
                    raw_text,
                    clean(row.get("last_error")),
                    created_at.isoformat(),
                    updated_at.isoformat(),
                ),
            )
            self.execute(
                """insert into trosa.email_message_receipts
                   (id,organization_id,legacy_user_id,provider_message_id,provider_thread_id,message_time,
                    sender_email,recipient_emails,subject,account_id,contact_method_id,
                    timeline_event_id,inbox_item_id,match_status,raw_payload,last_error,
                    created_at,updated_at)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (organization_id,legacy_user_id,provider_message_id) do update set
                    provider_thread_id=excluded.provider_thread_id,message_time=excluded.message_time,
                    sender_email=excluded.sender_email,recipient_emails=excluded.recipient_emails,
                    subject=excluded.subject,account_id=excluded.account_id,
                    contact_method_id=excluded.contact_method_id,timeline_event_id=excluded.timeline_event_id,
                    inbox_item_id=excluded.inbox_item_id,match_status=excluded.match_status,
                    raw_payload=excluded.raw_payload,last_error=excluded.last_error,
                    updated_at=excluded.updated_at""",
                (
                    compat_uuid(f"gmail-receipt:{user}:{provider}"),
                    ORG_ID,
                    user,
                    provider,
                    clean(row.get("provider_thread_id")),
                    parse_time(row.get("message_time")),
                    clean(row.get("sender_email")),
                    Jsonb(recipient_payload),
                    clean(row.get("subject")),
                    account,
                    contact,
                    timeline,
                    inbox_item,
                    match_status,
                    Jsonb(raw_payload),
                    clean(row.get("last_error")),
                    created_at,
                    updated_at,
                ),
            )

        for row in rows.get("communication_sources", []):
            number = self.legacy_id(row.get("id"))
            activity_id = self.legacy_id(row.get("activity_id"))
            if activity_id is None:
                self.issue(
                    f"trosa/{db_name}/communication_sources",
                    str(number or ""),
                    "INVALID_SOURCE_ACTIVITY_ID",
                    "Communication source has no valid activity id and was kept in the compatibility ledger only",
                    row,
                )
                continue
            existing_activity = self.cur.execute(
                """select id from trade_os_compat.communication_source_rows
                   where legacy_user_id=%s and activity_id=%s limit 1""",
                (user, activity_id),
            ).fetchone()
            if existing_activity:
                if number is not None and existing_activity[0] != number:
                    self.issue(
                        f"trosa/{db_name}/communication_sources",
                        str(number),
                        "DUPLICATE_SOURCE_ACTIVITY",
                        "Duplicate activity id reused the existing compatibility source row",
                        row,
                    )
                number = existing_activity[0]
            else:
                number = number or next_id("communication_source_rows")
                existing_id = self.cur.execute(
                    """select activity_id from trade_os_compat.communication_source_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and existing_id[0] != activity_id:
                    self.issue(
                        f"trosa/{db_name}/communication_sources",
                        str(number),
                        "SOURCE_ID_COLLISION",
                        "Communication source id is already used by another activity; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("communication_source_rows")
            self.execute("""insert into trade_os_compat.communication_source_rows
              (legacy_user_id,id,activity_id,channel,source_url,account,conversation_identity,adapter_version,extraction_scope,warnings,raw_payload,cleaned_payload,captured_at)
              values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set
              activity_id=excluded.activity_id,channel=excluded.channel,source_url=excluded.source_url,account=excluded.account,
              conversation_identity=excluded.conversation_identity,adapter_version=excluded.adapter_version,extraction_scope=excluded.extraction_scope,
              warnings=excluded.warnings,raw_payload=excluded.raw_payload,cleaned_payload=excluded.cleaned_payload,captured_at=excluded.captured_at""",
              (user,number,activity_id,clean(row.get("channel")),clean(row.get("source_url")),clean(row.get("account")),clean(row.get("conversation_identity")),clean(row.get("adapter_version")),clean(row.get("extraction_scope")),clean(row.get("warnings")) or "[]",clean(row.get("raw_payload")) or "{}",clean(row.get("cleaned_payload")),clean(row.get("captured_at"))))
            timeline=self.ref_target(db_name,"follow_up_logs",activity_id)
            if timeline:
                source_id=compat_uuid(f"communication-source:{user}:{activity_id}")
                self.execute("""insert into trosa.communication_sources(id,timeline_event_id,channel,source_url,account,conversation_identity,adapter_version,extraction_scope,warnings,raw_payload,cleaned_payload,captured_at)
                  values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(timeline_event_id) do update set raw_payload=excluded.raw_payload,cleaned_payload=excluded.cleaned_payload,captured_at=excluded.captured_at""",
                  (source_id,timeline,clean(row.get("channel")),clean(row.get("source_url")),clean(row.get("account")),clean(row.get("conversation_identity")),clean(row.get("adapter_version")),clean(row.get("extraction_scope")),Jsonb(json_value(row.get("warnings")) or []),Jsonb(json_value(row.get("raw_payload")) or {}),clean(row.get("cleaned_payload")),parse_time(row.get("captured_at"))))
                actual_source = self.cur.execute(
                    "select id from trosa.communication_sources where timeline_event_id=%s",
                    (timeline,),
                ).fetchone()
                self.row_ref(db_name,"communication_sources",number,actual_source[0] if actual_source else source_id)
            else:
                self.issue(
                    f"trosa/{db_name}/communication_sources",
                    str(number),
                    "MISSING_SOURCE_ACTIVITY",
                    "Communication source was retained in the compatibility ledger because its activity is unavailable",
                    row,
                )

        for row in rows.get("communication_source_items", []):
            number = self.legacy_id(row.get("id"))
            fingerprint = clean(row.get("source_fingerprint"))
            if not fingerprint:
                number = number or next_id("communication_source_item_rows")
                fingerprint = f"legacy:{user}:{number}"
                self.issue(
                    f"trosa/{db_name}/communication_source_items",
                    str(number),
                    "MISSING_SOURCE_FINGERPRINT",
                    "Source item had no fingerprint; a stable legacy fallback was used",
                    row,
                )
            activity_id = self.legacy_id(row.get("activity_id"))
            if activity_id is None:
                self.issue(
                    f"trosa/{db_name}/communication_source_items",
                    str(number or ""),
                    "INVALID_SOURCE_ITEM_ACTIVITY_ID",
                    "Source item has no valid activity id and was kept in the compatibility ledger only",
                    row,
                )
                continue
            existing_fingerprint = self.cur.execute(
                """select id from trade_os_compat.communication_source_item_rows
                   where legacy_user_id=%s and source_fingerprint=%s limit 1""",
                (user, fingerprint),
            ).fetchone()
            if existing_fingerprint:
                if number is not None and existing_fingerprint[0] != number:
                    self.issue(
                        f"trosa/{db_name}/communication_source_items",
                        str(number),
                        "DUPLICATE_SOURCE_FINGERPRINT",
                        "Duplicate source fingerprint reused the existing compatibility item",
                        row,
                    )
                number = existing_fingerprint[0]
            else:
                number = number or next_id("communication_source_item_rows")
                existing_id = self.cur.execute(
                    """select source_fingerprint from trade_os_compat.communication_source_item_rows
                       where legacy_user_id=%s and id=%s limit 1""",
                    (user, number),
                ).fetchone()
                if existing_id and existing_id[0] != fingerprint:
                    self.issue(
                        f"trosa/{db_name}/communication_source_items",
                        str(number),
                        "SOURCE_ITEM_ID_COLLISION",
                        "Source item id is already used by another fingerprint; a new compatibility id was allocated",
                        row,
                    )
                    number = next_id("communication_source_item_rows")
            self.execute("""insert into trade_os_compat.communication_source_item_rows
              (legacy_user_id,id,source_fingerprint,activity_id,message_time,direction,raw_text)
              values(%s,%s,%s,%s,%s,%s,%s) on conflict(legacy_user_id,id) do update set source_fingerprint=excluded.source_fingerprint,activity_id=excluded.activity_id,message_time=excluded.message_time,direction=excluded.direction,raw_text=excluded.raw_text""",
              (user,number,fingerprint,activity_id,clean(row.get("message_time")),clean(row.get("direction")) or "unknown",clean(row.get("raw_text"))))
            timeline = self.ref_target(db_name, "follow_up_logs", activity_id)
            source = self.cur.execute(
                """select s.id from trosa.communication_sources s
                   where s.timeline_event_id=%s limit 1""",
                (timeline,),
            ).fetchone() if timeline else None
            if source:
                self.execute("""insert into trosa.communication_source_items
                  (id,organization_id,legacy_user_id,communication_source_id,source_fingerprint,message_time,direction,raw_text)
                  values(%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (organization_id,legacy_user_id,source_fingerprint) do update set
                  communication_source_id=excluded.communication_source_id,message_time=excluded.message_time,
                  direction=excluded.direction,raw_text=excluded.raw_text""",
                  (compat_uuid(f"communication-item:{user}:{fingerprint}"), ORG_ID, user, source[0], fingerprint,
                   parse_time(row.get("message_time")), clean(row.get("direction")) or "unknown", clean(row.get("raw_text"))))
            else:
                self.issue(
                    f"trosa/{db_name}/communication_source_items",
                    str(number),
                    "MISSING_SOURCE_FOR_ITEM",
                    "Source item was retained in the compatibility ledger because its canonical communication source is unavailable",
                    row,
                )

        for row in rows.get("email_delivery_events", []):
            number = self.legacy_id(row.get("id")) or next_id("email_delivery_event_rows")
            email_value = clean(row.get("email"))
            existing_event = self.cur.execute(
                """select email from trade_os_compat.email_delivery_event_rows
                   where legacy_user_id=%s and id=%s limit 1""",
                (user, number),
            ).fetchone()
            if existing_event and existing_event[0] != email_value:
                self.issue(
                    f"trosa/{db_name}/email_delivery_events",
                    str(number),
                    "DELIVERY_EVENT_ID_COLLISION",
                    "Delivery event id is already used by another email; a new compatibility id was allocated",
                    row,
                )
                number = next_id("email_delivery_event_rows")
            event_type = clean(row.get("event_type")) or "unknown"
            occurred_at = parse_time(row.get("occurred_at"))
            if clean(row.get("occurred_at")) and occurred_at is None:
                self.issue(
                    f"trosa/{db_name}/email_delivery_events",
                    str(number),
                    "INVALID_DELIVERY_EVENT_DATE",
                    "Delivery event date is invalid; current time was used for the canonical required field",
                    row,
                )
            occurred_at = occurred_at or datetime.now(timezone.utc)
            contact_id = self.legacy_id(row.get("contact_id"))
            outreach_email_id = self.legacy_id(row.get("outreach_email_id"))
            contact = self.contact_method_for(db_name, contact_id, email_value)
            outreach = self.ref_target(db_name, "outreach_emails", outreach_email_id)
            payload = {key: json_value(value) for key, value in row.items()}
            occurred_text = clean(row.get("occurred_at")) or occurred_at.isoformat()
            self.execute(
                """insert into trade_os_compat.email_delivery_event_rows
                   (legacy_user_id,id,email,contact_id,outreach_email_id,event_type,smtp_code,
                    enhanced_status,diagnostic_text,remote_mta,message_id,source,occurred_at)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(legacy_user_id,id) do update set
                    email=excluded.email,contact_id=excluded.contact_id,
                    outreach_email_id=excluded.outreach_email_id,event_type=excluded.event_type,
                    smtp_code=excluded.smtp_code,enhanced_status=excluded.enhanced_status,
                    diagnostic_text=excluded.diagnostic_text,remote_mta=excluded.remote_mta,
                    message_id=excluded.message_id,source=excluded.source,occurred_at=excluded.occurred_at""",
                (
                    user,
                    number,
                    email_value,
                    contact_id,
                    outreach_email_id,
                    event_type,
                    clean(row.get("smtp_code")),
                    clean(row.get("enhanced_status")),
                    clean(row.get("diagnostic_text")),
                    clean(row.get("remote_mta")),
                    clean(row.get("message_id")),
                    clean(row.get("source")) or "manual",
                    occurred_text,
                ),
            )
            self.execute(
                """insert into trosa.email_delivery_events
                   (id,organization_id,contact_method_id,outreach_message_id,event_type,smtp_code,
                    enhanced_status,diagnostic_text,remote_mta,provider_message_id,source,
                    occurred_at,legacy_payload)
                   values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict(id) do update set
                    contact_method_id=excluded.contact_method_id,outreach_message_id=excluded.outreach_message_id,
                    event_type=excluded.event_type,smtp_code=excluded.smtp_code,
                    enhanced_status=excluded.enhanced_status,diagnostic_text=excluded.diagnostic_text,
                    remote_mta=excluded.remote_mta,provider_message_id=excluded.provider_message_id,
                    source=excluded.source,occurred_at=excluded.occurred_at,legacy_payload=excluded.legacy_payload""",
                (
                    compat_uuid(f"email-delivery:{user}:{number}"),
                    ORG_ID,
                    contact,
                    outreach,
                    event_type,
                    clean(row.get("smtp_code")),
                    clean(row.get("enhanced_status")),
                    clean(row.get("diagnostic_text")),
                    clean(row.get("remote_mta")),
                    clean(row.get("message_id")),
                    clean(row.get("source")) or "manual",
                    occurred_at,
                    Jsonb(payload),
                ),
            )

    def register_batch(self, source: str, path: str, source_hash: str, rows: int) -> None:
        self.batch_id = uid(f"batch:{source}:{source_hash}")
        self.execute("""insert into audit.import_batches(id,organization_id,source_name,source_path,source_sha256,source_rows) values (%s,%s,%s,%s,%s,%s) on conflict (id) do update set source_name=excluded.source_name,source_path=excluded.source_path,source_sha256=excluded.source_sha256,source_rows=excluded.source_rows""", (self.batch_id,ORG_ID,source,path,source_hash,rows))

    def import_sela(self) -> None:
        candidates = json.loads((self.sela_dir / 'candidates.json').read_text(encoding='utf-8'))
        feedback = json.loads((self.sela_dir / 'feedback_events.json').read_text(encoding='utf-8'))
        memory = json.loads((self.sela_dir / 'search_memory.json').read_text(encoding='utf-8'))
        if not isinstance(candidates, list):
            raise ValueError('sela candidates.json must contain a list')
        if not isinstance(feedback, list):
            raise ValueError('sela feedback_events.json must contain a list')
        if not isinstance(memory, dict):
            raise ValueError('sela search_memory.json must contain an object')
        sources = [
            ('sela/candidates.json', self.sela_dir / 'candidates.json', candidates),
            ('sela/feedback_events.json', self.sela_dir / 'feedback_events.json', feedback),
            ('sela/search_memory.json', self.sela_dir / 'search_memory.json', memory),
        ]
        batch_ids = {}
        for source, path, payload in sources:
            values = payload if isinstance(payload, list) else [{"key": k, "value": v} for k, v in payload.items()]
            self.register_batch(source, str(path), sha256(path), len(values))
            batch_ids[source] = self.batch_id
            for n, row in enumerate(values, 1):
                if not isinstance(row, dict):
                    self.issue(source, str(n), 'INVALID_ROW', 'Sela source row is not an object', {'value': row})
                    continue
                self.archive(source, clean(row.get('id')) or clean(row.get('key')) or str(n), row, n)
        self.batch_id = batch_ids['sela/candidates.json']
        for row in candidates:
            if not isinstance(row, dict):
                self.issue('sela/candidates.json', 'unknown', 'INVALID_ROW', 'Candidate is not an object')
                continue
            candidate_id = clean(row.get('id'))
            if not candidate_id:
                self.issue('sela/candidates.json', 'unknown', 'INVALID_CANDIDATE_ID', 'Candidate has no id', row)
                continue
            company_id = self.company(
                row.get('company'),
                row.get('domain') or row.get('website'),
                row.get('country'),
                row.get('city'),
                row.get('business_type'),
                source_identity=f"sela:candidate:{candidate_id}",
            )
            contact_id = self.email(company_id, row.get('email'), evidence={"source_url": row.get('email_source_url', ''), "contact_evidence": row.get('contact_evidence', '')})
            prospect_id = uid(f"prospect:{candidate_id}")
            self.execute("""insert into sela.prospects(id,organization_id,company_id,contact_method_id,legacy_candidate_id,campaign,source_run_id,qualification_status,research_status,confidence,do_not_contact,imported_at,updated_at,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,legacy_candidate_id) do update set legacy_payload=excluded.legacy_payload,updated_at=excluded.updated_at""", (prospect_id, ORG_ID, company_id, contact_id, candidate_id, clean(row.get('campaign')), clean(row.get('source_run')), clean(row.get('status')), clean(row.get('research_status')), clean(row.get('confidence')), legacy_bool(row.get('do_not_contact')), parse_time(row.get('imported_at')), parse_time(row.get('updated_at')), Jsonb(row)))
            actual = self.cur.execute("select id from sela.prospects where organization_id=%s and legacy_candidate_id=%s", (ORG_ID, candidate_id)).fetchone()
            if not actual:
                self.issue('sela/candidates.json', candidate_id, 'PROSPECT_NOT_CREATED', 'Candidate projection could not be resolved', row)
                continue
            prospect_id = actual[0]
            self.prospects[candidate_id] = prospect_id
            self.execute("insert into sela.prospect_research(prospect_id,qualification_method,qualification_reason,reason,angle,supplier_pivot,site_hygiene,research_reason) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (prospect_id) do update set reason=excluded.reason", (prospect_id, clean(row.get('qualification_method')), clean(row.get('qualification_reason')), clean(row.get('reason')), clean(row.get('angle')), clean(row.get('supplier_pivot')), clean(row.get('site_hygiene')), clean(row.get('research_reason'))))
            self.execute("insert into sela.outreach_messages(id,prospect_id,subject,body,message_variant,provider_draft_id,provider_thread_id,provider_message_id,sent_at,last_send_attempt_at,last_send_error,auto_send_blocked,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set legacy_payload=excluded.legacy_payload", (uid(f"sela-message:{candidate_id}"), prospect_id, clean(row.get('subject')), clean(row.get('email_draft')), clean(row.get('message_variant')), clean(row.get('gmail_draft_id')), clean(row.get('gmail_thread_id')), clean(row.get('gmail_message_id')), parse_time(row.get('sent_at')), parse_time(row.get('last_gmail_send_attempt_at')), clean(row.get('last_gmail_send_error')), legacy_bool(row.get('local_gmail_auto_send_blocked')), Jsonb(row)))
            # Evidence is a first-class source record, not just a nested
            # field on the prospect payload.  Replace only the rows produced
            # by this source so manually captured evidence survives a repeat
            # import.
            self.execute(
                """delete from sela.prospect_evidence
                   where prospect_id=%s
                     and raw_payload->>'_import_source'='sela/candidates.json'""",
                (prospect_id,),
            )
            for evidence_index, evidence in enumerate(sela_evidence_entries(row), 1):
                evidence_payload = dict(evidence.get("raw_payload") or {})
                evidence_payload["_import_source"] = "sela/candidates.json"
                evidence_payload["_legacy_candidate_id"] = candidate_id
                self.execute(
                    """insert into sela.prospect_evidence
                       (id,prospect_id,evidence_type,source_url,source_file,excerpt,captured_at,raw_payload)
                       values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set
                       evidence_type=excluded.evidence_type,source_url=excluded.source_url,
                       source_file=excluded.source_file,excerpt=excluded.excerpt,
                       captured_at=excluded.captured_at,raw_payload=excluded.raw_payload""",
                    (
                        uid(f"sela-evidence:{candidate_id}:{evidence_index}"),
                        prospect_id,
                        evidence.get("evidence_type", "candidate_source"),
                        evidence.get("source_url", ""),
                        evidence.get("source_file", "") or "sela/candidates.json",
                        evidence.get("excerpt", ""),
                        evidence.get("captured_at"),
                        Jsonb(evidence_payload),
                    ),
                )
        self.batch_id = batch_ids['sela/feedback_events.json']
        for n, row in enumerate(feedback, 1):
            if not isinstance(row, dict):
                self.issue('sela/feedback_events.json', str(n), 'INVALID_ROW', 'Feedback row is not an object', {'value': row})
                continue
            occurred = parse_time(row.get('at'))
            if clean(row.get('at')) and occurred is None:
                self.issue('sela/feedback_events.json', str(n), 'INVALID_EVENT_DATE', 'Feedback date is invalid; current time used for required projection', row)
            candidate_id = clean(row.get('candidate_id'))
            prospect_id = self.prospects.get(candidate_id)
            if candidate_id and prospect_id is None:
                self.issue('sela/feedback_events.json', str(n), 'MISSING_CANDIDATE', 'Feedback row has no matching candidate; event remains unassigned', row)
            self.execute("""insert into sela.prospect_events(id,organization_id,prospect_id,legacy_candidate_id,occurred_at,event_type,company_text,campaign,market,business_type,confidence,contact_route,email_type,email_evidence_tier,message_variant,outreach_status_snapshot,detail,legacy_row_number,source_batch_id,legacy_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,source_batch_id,legacy_row_number) do update set prospect_id=excluded.prospect_id,legacy_candidate_id=excluded.legacy_candidate_id,occurred_at=excluded.occurred_at,event_type=excluded.event_type,company_text=excluded.company_text,campaign=excluded.campaign,market=excluded.market,business_type=excluded.business_type,confidence=excluded.confidence,contact_route=excluded.contact_route,email_type=excluded.email_type,email_evidence_tier=excluded.email_evidence_tier,message_variant=excluded.message_variant,outreach_status_snapshot=excluded.outreach_status_snapshot,detail=excluded.detail,legacy_payload=excluded.legacy_payload""", (uid(f"sela-event:{self.batch_id}:{n}"), ORG_ID, prospect_id, candidate_id, occurred or datetime.now(timezone.utc), clean(row.get('event')), clean(row.get('company')), clean(row.get('campaign')), clean(row.get('market')), clean(row.get('business_type')), clean(row.get('confidence')), clean(row.get('contact_route')), clean(row.get('email_type')), clean(row.get('email_evidence_tier')), clean(row.get('message_variant')), clean(row.get('outreach_status')), clean(row.get('detail')), n, self.batch_id, Jsonb(row)))
        activity_path=self.sela_dir/'activity_events.sqlite3'
        con=sqlite3.connect(f"file:{activity_path}?mode=ro",uri=True); con.row_factory=sqlite3.Row
        activity_count=con.execute('select count(*) from activity_events').fetchone()[0]
        self.register_batch('sela/activity_events.sqlite3',str(activity_path),sha256(activity_path),activity_count)
        for row in con.execute('select * from activity_events'):
            row = dict(row)
            activity_id = self.legacy_id(row.get('id'))
            if activity_id is None:
                self.issue('sela/activity_events.sqlite3', clean(row.get('id')), 'INVALID_ACTIVITY_ID', 'Activity row has no positive integer id', row)
                continue
            self.archive('sela/activity_events.sqlite3', str(activity_id), row, activity_id)
            candidate_id = clean(row.get('candidate_id'))
            prospect_id = self.prospects.get(candidate_id)
            if candidate_id and prospect_id is None:
                self.issue('sela/activity_events.sqlite3', str(activity_id), 'MISSING_CANDIDATE', 'Activity row has no matching candidate; event remains unassigned', row)
            self.execute("insert into sela.run_activity_events(id,organization_id,legacy_activity_id,run_id,campaign_id,legacy_candidate_id,prospect_id,kind,status,message,details,business_progress,occurred_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,legacy_activity_id) do update set prospect_id=excluded.prospect_id,details=excluded.details,occurred_at=excluded.occurred_at", (uid(f"sela-activity:{activity_id}"), ORG_ID, activity_id, clean(row.get('run_id')), clean(row.get('campaign_id')), candidate_id, prospect_id, clean(row.get('kind')), clean(row.get('status')), clean(row.get('message')), Jsonb(json_value(row.get('details_json')) or {}), legacy_bool(row.get('business_progress')), parse_time(row.get('created_at')) or datetime.now(timezone.utc)))
        con.close()
        for key,value in memory.items():
            values=value if isinstance(value,list) else [{"key":k,"value":v} for k,v in (value.items() if isinstance(value,dict) else [(key,value)])]
            for n,row in enumerate(values,1):
                payload = row if isinstance(row, dict) else {"value": row}
                self.execute("insert into sela.search_memory_entries(id,organization_id,entry_type,legacy_row_number,run_id,occurred_at,payload) values (%s,%s,%s,%s,%s,%s,%s) on conflict (organization_id,entry_type,legacy_row_number) do update set payload=excluded.payload", (uid(f"memory:{key}:{n}"), ORG_ID, clean(key), n, clean(payload.get('run_id')), parse_time(payload.get('at')), Jsonb(payload)))

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
                for table in (
                    'core.companies','core.people','core.contact_methods','trosa.accounts',
                    'trosa.tasks','trosa.timeline_events','trosa.outreach_messages',
                    'trosa.communication_sources','trosa.communication_source_items',
                    'trosa.weekly_reports','trosa.email_message_receipts',
                    'trosa.email_delivery_events','sela.prospects','sela.prospect_evidence',
                    'sela.prospect_events','sela.run_activity_events','sela.search_memory_entries',
                    'audit.import_batches','audit.integration_receipts',
                    'audit.agent_actions','audit.undo_snapshots','audit.imported_activity_rows',
                    'audit.import_unmatched_customers',
                    'audit.agent_gateway_idempotency','audit.legacy_records',
                    'trade_os_compat.app_settings','trade_os_compat.integration_sync_receipt_rows',
                    'trade_os_compat.operation_log_rows','trade_os_compat.agent_proposal_rows',
                    'trade_os_compat.agent_gateway_rows','trade_os_compat.agent_action_rows',
                    'trade_os_compat.undo_action_rows','trade_os_compat.import_batch_rows',
                    'trade_os_compat.imported_activity_row_rows',
                    'trade_os_compat.import_unmatched_customer_rows',
                    'trade_os_compat.email_delivery_event_rows',
                    'trade_os_compat.gmail_message_state_rows',
                    'trade_os_compat.communication_source_rows',
                    'trade_os_compat.communication_source_item_rows',
                    'trosa.email_verifications','trosa.email_verification_jobs',
                    'trosa.email_domain_probes','trosa.email_logs',
                ):
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
