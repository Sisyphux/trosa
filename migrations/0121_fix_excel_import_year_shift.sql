-- Kelley's weekly-report history (Excel recovery, legacy follow-up ids 469-584)
-- was imported with every date two years too late: the sheet columns run left to
-- right from 2024 to 2025, but the import anchored the first column at 2026, so
-- the records landed between 2026-07 and 2027-12 (90 of them in the future).
-- Order, month and day were right in every row (content leads with the same
-- month.day) -- only the year was off by exactly two.  Checked per customer:
-- after the shift all 115 rows are in sheet order, none is after the import day
-- (2026-09-03), and each month agrees with the month written in the content.
--
-- Id 515 ("4.8 展会见面", 2026-04-15) was already right and is left alone.
-- Amy has different rows with the same legacy ids; only Kelley's are touched.
-- The old date is kept in the event payload (date_corrected_from) so the change
-- can be undone.  Only the year of occurred_at moves (UTC wall clock, so the
-- local calendar day is unchanged); the payload copy of follow_date follows it.
BEGIN;

UPDATE trosa.timeline_events e
   SET occurred_at = ((e.occurred_at AT TIME ZONE 'UTC') - interval '2 years') AT TIME ZONE 'UTC',
       payload = coalesce(e.payload, '{}'::jsonb) || jsonb_build_object(
         'follow_date', to_char((trosa.compat_local_date(e.occurred_at)::date - interval '2 years'), 'YYYY-MM-DD'),
         'date_corrected_from', trosa.compat_local_date(e.occurred_at))
  FROM trosa.legacy_row_refs r
 WHERE r.table_name = 'follow_up_logs'
   AND r.legacy_user_id = 'kelley'
   AND r.legacy_id BETWEEN 469 AND 584
   AND r.legacy_id <> 515
   AND e.id = r.target_id
   AND e.source_module = 'trosa'
   AND e.created_at < timestamptz '2026-09-04 00:00:00+08'
   AND coalesce(e.payload ->> 'date_corrected_from', '') = '';

COMMIT;
