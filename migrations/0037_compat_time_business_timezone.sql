-- Make naive compatibility timestamps deterministic across PostgreSQL servers.
--
-- The runtime historically passed Shanghai wall-clock strings such as
-- ``2026-09-17 23:30:00`` (Python ``datetime.now()`` and ``_calendar_now_text()``)
-- into ``trosa.compat_time``.  The old definition simply did ``value::timestamptz``,
-- so a naive value was interpreted in the session ``TimeZone``.  The rehearsal
-- cluster inherits ``Asia/Shanghai`` from the developer machine, but the
-- production Docker service has no TZ configured and therefore resolves naive
-- values as UTC.  The same write then lands eight hours away from the intended
-- business instant, and ``trosa.compat_local_date`` (which is hardcoded to
-- ``Asia/Shanghai``) can roll it onto the wrong calendar day for any local time
-- at or after 16:00.
--
-- ``trosa.compat_time`` now interprets an explicit offset/``Z`` suffix as an
-- absolute instant, and every offset-less value as Trosa's business timezone
-- (``Asia/Shanghai``) regardless of the server session.  The application
-- connection pool additionally pins ``TimeZone`` to ``Asia/Shanghai`` so
-- ``timestamptz::text`` projections and ``CURRENT_TIMESTAMP`` text casts keep
-- the same local shape as the legacy SQLite runtime.
--
-- This is a forward-only function redefinition and changes no stored row.
BEGIN;

CREATE OR REPLACE FUNCTION trosa.compat_time(value text)
RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v text := trim(coalesce(value, ''));
BEGIN
    IF v = '' THEN
        RETURN NULL;
    END IF;
    -- An explicit UTC designator or numeric offset is an absolute instant.
    IF v ~ '(?:[zZ]|[+-][0-9]{2}(?::?[0-9]{2})?)$' THEN
        RETURN v::timestamptz;
    END IF;
    -- Offset-less text is Trosa business wall-clock time, not the server TZ.
    RETURN v::timestamp AT TIME ZONE 'Asia/Shanghai';
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

COMMIT;
