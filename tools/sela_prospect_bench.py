#!/usr/bin/env python3
"""S9 baseline / after measurement for the Sela prospect read path.

Read-only against a local, isolated database (a throwaway SQLite directory, or
the PostgreSQL rehearsal cluster).  It seeds a representative prospect pool,
then drives the *real* HTTP read routes through the Flask test client, timing
them and counting the SQL statements each call issues.  The S9 acceptance
table must be measured with this file, not guessed.

Usage (SQLite)::

    python3 tools/sela_prospect_bench.py --rows 1500 --repeats 15

Usage (PostgreSQL drill DB, inside the rehearsal env)::

    python3 tools/postgres_rehearsal.py start && \\
      python3 tools/postgres_rehearsal.py init && \\
      python3 tools/postgres_rehearsal.py migrate && \\
      eval "$(python3 tools/postgres_rehearsal.py env --shell)" && \\
      python3 tools/sela_prospect_bench.py --backend postgres --rows 1500 --repeats 15

Nothing here writes to a shared or production database: SQLite mode owns its
temporary directory and PostgreSQL mode refuses a non-loopback DSN.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

TOKEN = "test-sela-bench-token"
RETRYABLE = ()


def load_app():
    spec = importlib.util.spec_from_file_location("trosa_sela_bench_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.schedule_safety_backup = lambda *_args, **_kwargs: None
    return module


class CountingConnection:
    """Proxy that forwards to a DB-API connection and counts statements."""

    def __init__(self, conn, counter):
        self._conn = conn
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter[0] += 1
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._counter[0] += 1
        return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self._counter[0] += 1
        return self._conn.executescript(*args, **kwargs)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def seed_token():
    value = json.dumps(
        {
            "token_sha256": hashlib.sha256(TOKEN.encode("utf-8")).hexdigest(),
            "enabled": True,
            "user": "hamid",
        }
    )
    conn = db.get_system_db()
    try:
        if db.postgres_mode():
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("integration_token:sela:hamid", value, "2026-10-01 10:00:00"),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, datetime('now', 'localtime'))",
                ("integration_token:sela:hamid", value),
            )
        conn.commit()
    finally:
        conn.close()


def seed_pool(rows, *, with_outreach_ratio=0.7, verified_ratio=0.5, stub_ratio=0.15):
    """Seed ``rows`` cold prospects with a realistic mix of facts."""
    db.set_db_user("hamid")
    conn = db.get_db()
    try:
        cur = conn.cursor()
        for index in range(rows):
            name = f"Bench Company {index:05d}"
            website = f"https://bench{index}.example/"
            has_email = index % 100 >= int(stub_ratio * 100)
            cur.execute(
                "INSERT INTO customers (name, company, website, country, field, status, "
                "business_stage, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    name,
                    website,
                    "Brazil" if index % 2 else "Germany",
                    "Acrylic sheet fabricator",
                    "未建联",
                    "",
                    "2026-10-01 10:00:00",
                    "2026-09-09 10:00:00",
                ),
            )
            customer_id = cur.lastrowid
            cur.execute(
                "INSERT INTO agent_prospect_profiles "
                "(legacy_user_id, source, source_id, customer_id, research_json, "
                " transport_json, created_at, updated_at) "
                "VALUES (?, 'sela', ?, ?, ?, ?, ?, ?)",
                (
                    "hamid",
                    f"bench-{index:05d}",
                    customer_id,
                    json.dumps({"qualification_status": "READY TO CONTACT", "confidence": "HIGH"}),
                    json.dumps({"gmail_draft_id": f"draft-{index}"} if index % 3 == 0 else {}),
                    "2026-09-09 10:00:00",
                    "2026-10-01 10:00:00",
                ),
            )
            if has_email:
                email = f"buyer{index}@bench{index}.example"
                cur.execute(
                    "INSERT INTO contacts (customer_id, name, title, email, is_primary, created_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (customer_id, f"Buyer {index}", "Purchasing", email, "2026-09-09 10:00:00"),
                )
                if index % 100 < int(verified_ratio * 100):
                    cur.execute(
                        "INSERT OR REPLACE INTO email_verifications "
                        "(email, normalized_email, domain, deliverability_status, confidence, checked_at) "
                        "VALUES (?, ?, ?, 'likely_deliverable', 'high', ?)",
                        (email, email.lower(), f"bench{index}.example", "2026-10-01 10:00:00"),
                    )
                if index % 100 < int(with_outreach_ratio * 100):
                    sent = index % 4 != 0  # a quarter stay seeded drafts
                    cur.execute(
                        "INSERT INTO outreach_emails "
                        "(customer_id, subject, content, sent_date, reply_status, "
                        " external_source, external_id, external_updated_at, created_at) "
                        "VALUES (?, ?, ?, ?, 'pending', 'sela', ?, ?, ?)",
                        (
                            customer_id,
                            "Acrylic sheet supply",
                            "Hello, we can support acrylic sheet sourcing.",
                            "2026-09-20" if sent else "",
                            f"bench-{index:05d}",
                            "2026-09-20" if sent else "",
                            "2026-09-09 10:00:00",
                        ),
                    )
        conn.commit()
    finally:
        conn.close()


def seed_pool_postgres(rows, *, with_outreach_ratio=0.7, verified_ratio=0.5, stub_ratio=0.15):
    """Seed the rehearsal database through the canonical write boundary."""
    import trosa_domain

    db.set_db_user("hamid")
    conn = db.get_db()
    try:
        for index in range(rows):
            name = f"Bench Company {index:05d}"
            website = f"https://bench{index}.example/"
            customer_id = trosa_domain.create_customer(
                conn,
                values={
                    "name": name,
                    "company": name,
                    "website": website,
                    "country": "Brazil" if index % 2 else "Germany",
                    "field": "Acrylic sheet fabricator",
                    "status": "未建联",
                    "import_source": "sela-bench",
                },
            )
            has_email = index % 100 >= int(stub_ratio * 100)
            if has_email:
                email = f"buyer{index}@bench{index}.example"
                trosa_domain.create_contact(
                    conn,
                    customer_id=customer_id,
                    values={"name": f"Buyer {index}", "title": "Purchasing", "email": email,
                            "is_primary": True, "contact_type": "person"},
                    created_at="2026-09-09 10:00:00",
                )
                if index % 100 < int(verified_ratio * 100):
                    conn.execute(
                        "INSERT INTO trosa.email_verifications "
                        "(organization_id, legacy_user_id, email, normalized_email, domain, "
                        " deliverability_status, confidence, checked_at) "
                        "VALUES (trosa.compat_org_id(), trosa.compat_current_user(), ?, ?, ?, "
                        " 'likely_deliverable', 'high', ?) "
                        "ON CONFLICT (organization_id, legacy_user_id, normalized_email) DO NOTHING",
                        (email, email.lower(), f"bench{index}.example", "2026-10-01 10:00:00"),
                    )
                if index % 100 < int(with_outreach_ratio * 100):
                    sent = index % 4 != 0
                    trosa_domain.create_outreach_message(
                        conn,
                        customer_id=customer_id,
                        subject="Acrylic sheet supply",
                        content="Hello, we can support acrylic sheet sourcing.",
                        sent_on="2026-09-20" if sent else "",
                        reply_status="pending",
                        created_at="2026-09-09 10:00:00",
                    )
            conn.execute(
                "INSERT INTO trosa.agent_prospect_profiles "
                "(legacy_user_id, source, source_id, customer_id, research_json, transport_json, "
                " created_at, updated_at) VALUES ('hamid', 'sela', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (organization_id, legacy_user_id, source, source_id) DO NOTHING",
                (
                    f"bench-{index:05d}",
                    customer_id,
                    json.dumps({"qualification_status": "READY TO CONTACT", "confidence": "HIGH"}),
                    json.dumps({"gmail_draft_id": f"draft-{index}"} if index % 3 == 0 else {}),
                    "2026-09-09 10:00:00",
                    "2026-10-01 10:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def measure(app_module, scenarios, repeats):
    client = app_module.app.test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    results = {}
    for name, run in scenarios:
        samples = []
        queries = []
        rows = []
        for _ in range(repeats):
            counter = [0]
            real_get_db = app_module.get_db

            def counting_get_db(counter=counter, real_get_db=real_get_db):
                return CountingConnection(real_get_db(), counter)

            app_module.get_db = counting_get_db
            try:
                started = time.perf_counter()
                observed = run(client, headers)
                samples.append((time.perf_counter() - started) * 1000.0)
            finally:
                app_module.get_db = real_get_db
            queries.append(counter[0])
            rows.append(observed)
        samples.sort()

        def pct(values, p):
            if not values:
                return 0.0
            k = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
            return values[k]

        results[name] = {
            "runs": repeats,
            "p50_ms": round(statistics.median(samples), 1),
            "p95_ms": round(pct(samples, 95), 1),
            "min_ms": round(samples[0], 1),
            "max_ms": round(samples[-1], 1),
            "queries": queries[0],
            "rows": rows[0],
        }
    return results


def full_read(client, headers):
    after = 0
    total_rows = 0
    pages = 0
    while True:
        response = client.get(
            f"/api/integrations/sela/prospects?after={after}&limit=100", headers=headers
        )
        if response.status_code != 200:
            raise SystemExit(f"full read failed: {response.status_code} {response.get_data(as_text=True)}")
        data = response.get_json()
        total_rows += len(data["prospects"])
        pages += 1
        after = data.get("next_after")
        if after is None or pages > 100:
            break
    return f"{total_rows} rows / {pages} pages"


def single_page(client, headers):
    response = client.get("/api/integrations/sela/prospects?after=0&limit=50", headers=headers)
    if response.status_code != 200:
        raise SystemExit(f"single page failed: {response.status_code}")
    return f"{len(response.get_json()['prospects'])} rows"


def verify_equivalence(app_module, *, sample_pages=3, limit=100):
    """The batched page view must equal the per-row view, field for field.

    Returns the number of prospects compared.  The batching change is only
    legitimate if it returns the same rows, so this is asserted rather than
    assumed.
    """
    with app_module.app.app_context():
        db.set_db_user("hamid")
        conn = app_module.get_db()
        try:
            relation = "trosa.agent_prospect_profiles" if db.postgres_mode() else "agent_prospect_profiles"
            scope = "organization_id=trosa.compat_org_id() AND " if db.postgres_mode() else ""
            profiles = conn.execute(
                f"SELECT * FROM {relation} WHERE {scope}legacy_user_id=? AND source=? ORDER BY id",
                ("hamid", "sela"),
            ).fetchall()
            checked = 0
            for start in range(0, min(len(profiles), sample_pages * limit), limit):
                page_rows = profiles[start:start + limit]
                page = app_module._SelaProspectPage(conn, page_rows)
                for row in page_rows:
                    batched = app_module._sela_prospect_view(conn, row, context=page)
                    lazy = app_module._sela_prospect_view(conn, row)
                    if batched != lazy:
                        keys = sorted(set(batched or {}) | set(lazy or {}))
                        differing = [k for k in keys if (batched or {}).get(k) != (lazy or {}).get(k)]
                        raise SystemExit(
                            f"page view differs from per-row view at profile "
                            f"{dict(row).get('source_id')}: {differing}"
                        )
                    checked += 1
            return checked
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1500)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    args = parser.parse_args()

    if args.backend == "postgres":
        dsn = os.environ.get("TRADE_OS_DATABASE_URL", "")
        if "127.0.0.1" not in dsn and "localhost" not in dsn:
            raise SystemExit("refusing PostgreSQL mode without a loopback TRADE_OS_DATABASE_URL")
        if os.environ.get("TRADE_OS_DATA_BACKEND") != "postgres":
            raise SystemExit("TRADE_OS_DATA_BACKEND must be 'postgres' in PostgreSQL mode")
        seeded_rows = int(os.environ.get("SELA_BENCH_SEEDED_ROWS") or 0)
        tempdir = None
        db.init_postgres_store()
    else:
        tempdir = tempfile.TemporaryDirectory()
        db.DB_DIR = tempdir.name
        os.environ.pop("CRM_SEED_DEMO_DATA", None)
        db.init_all_dbs()
        seeded_rows = 0

    try:
        seed_token()
        if args.backend == "sqlite":
            seed_pool(args.rows)
        elif not seeded_rows:
            seed_pool_postgres(args.rows)
            os.environ["SELA_BENCH_SEEDED_ROWS"] = str(args.rows)
        app_module = load_app()
        checked = verify_equivalence(app_module)
        scenarios = [
            ("full_read", full_read),
            ("single_page_50", single_page),
        ]
        results = measure(app_module, scenarios, args.repeats)
    finally:
        if tempdir is not None:
            db.cancel_safety_backup()
            db.set_db_user(None)
            tempdir.cleanup()

    header = f"{'scenario':<18}{'p50(ms)':>10}{'p95(ms)':>10}{'queries':>10}  observation"
    print(f"backend={args.backend} rows_seeded={args.rows} repeats={args.repeats}")
    print(f"equivalence: {checked} prospects identical between batched and per-row view")
    print(header)
    print("-" * len(header))
    for name, row in results.items():
        print(
            f"{name:<18}{row['p50_ms']:>10}{row['p95_ms']:>10}{row['queries']:>10}  {row['rows']}"
        )


if __name__ == "__main__":
    main()
