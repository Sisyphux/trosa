-- One customer keeps one open follow-up per date: heal legacy duplicates,
-- enforce the invariant, and merge same-day writes at the compat boundary.
--
-- Today previously rendered one row per task, so a customer with three open
-- follow-ups for the same date appeared three times.  The application now
-- merges on create/reschedule/edit, but rows created earlier (bulk imports,
-- direct compat writes, reschedules onto an occupied date) remain.  This
-- migration merges those survivors and makes the database reject new ones.
BEGIN;

-- 1. Merge existing same-account same-day open follow-ups.  The earliest
-- created row survives; reasons are joined with ' / '; the rest become
-- completed tombstones so audit history is preserved.
DO $$
DECLARE
    r record;
    survivor_id uuid;
    merged_reason text;
    dup record;
    merged_groups int := 0;
    merged_rows int := 0;
BEGIN
    FOR r IN
        SELECT t.account_id,
               (t.due_at AT TIME ZONE 'Asia/Shanghai')::date AS due_day,
               count(*) AS n
         FROM trosa.tasks t
         WHERE t.status='open' AND t.task_type='follow_up' AND t.due_at IS NOT NULL
         GROUP BY t.account_id, ((t.due_at AT TIME ZONE 'Asia/Shanghai')::date)
        HAVING count(*) > 1
    LOOP
        merged_groups := merged_groups + 1;
        merged_rows := merged_rows + (r.n - 1);
        SELECT t.id INTO survivor_id
          FROM trosa.tasks t
         WHERE t.status='open' AND t.task_type='follow_up' AND t.due_at IS NOT NULL
           AND t.account_id=r.account_id
           AND (t.due_at AT TIME ZONE 'Asia/Shanghai')::date=r.due_day
         ORDER BY t.created_at ASC, t.id ASC
         LIMIT 1;

        SELECT string_agg(DISTINCT trim(t.reason), ' / ' ORDER BY trim(t.reason))
          INTO merged_reason
          FROM trosa.tasks t
         WHERE t.status='open' AND t.task_type='follow_up' AND t.due_at IS NOT NULL
           AND t.account_id=r.account_id
           AND (t.due_at AT TIME ZONE 'Asia/Shanghai')::date=r.due_day
           AND nullif(trim(t.reason), '') IS NOT NULL;

        UPDATE trosa.tasks
           SET reason=coalesce(substring(merged_reason from 1 for 2000), ''),
               updated_at=now()
         WHERE id=survivor_id;

        FOR dup IN
            SELECT t.id
              FROM trosa.tasks t
             WHERE t.status='open' AND t.task_type='follow_up'
               AND t.account_id=r.account_id
               AND (t.due_at AT TIME ZONE 'Asia/Shanghai')::date=r.due_day
               AND t.id<>survivor_id
        LOOP
            UPDATE trosa.tasks
               SET status='done', completed_at=coalesce(completed_at, now()),
                   legacy_payload=coalesce(legacy_payload, '{}'::jsonb)
                       || jsonb_build_object('merged_into', survivor_id::text,
                                             'merged_at', now()::text),
                   updated_at=now()
             WHERE id=dup.id;
        END LOOP;

    END LOOP;
    RAISE NOTICE 'one_follow_up_per_day: merged % duplicate rows in % customer-day groups',
        merged_rows, merged_groups;
END
$$;

-- 2. Enforce the invariant for all future writers.
CREATE UNIQUE INDEX IF NOT EXISTS trosa_tasks_one_open_follow_up_per_day_idx
    ON trosa.tasks (account_id, ((due_at AT TIME ZONE 'Asia/Shanghai')::date))
    WHERE due_at IS NOT NULL AND status='open' AND task_type='follow_up';

-- 3. Merge same-day compat writes instead of inserting a duplicate row.
-- This keeps bulk imports, undo restores and legacy clients on the same
-- one-row-per-day contract as the application write boundary.
CREATE OR REPLACE FUNCTION trosa.compat_reminders_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_legacy_user text:=trosa.compat_current_user();
    v_legacy_id bigint;
    v_account_id uuid;
    v_target_id uuid;
    v_due timestamptz;
    v_is_new boolean := false;
    v_survivor_target uuid;
    v_survivor_legacy bigint;
    v_survivor_title text;
    v_survivor_content text;
    v_survivor_reason text;
    v_merged_reason text;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT lr.target_id INTO v_target_id FROM trosa.legacy_row_refs lr
          WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
            AND lr.table_name='reminders' AND lr.legacy_id=OLD.id;
        DELETE FROM trosa.legacy_row_refs
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_legacy_user
           AND table_name='reminders' AND legacy_id=OLD.id;
        IF v_target_id IS NOT NULL AND (
            EXISTS (SELECT 1 FROM trosa.web_monitor_observations WHERE task_id=v_target_id)
            OR EXISTS (SELECT 1 FROM trosa.legacy_row_refs WHERE target_id=v_target_id)
        ) THEN
            -- web_monitor_observations.task_id is a NO ACTION foreign key.
            -- Retain a completed tombstone when another canonical row still
            -- points at the task, while hiding it from the legacy view.
            UPDATE trosa.tasks
               SET status='done', completed_at=coalesce(completed_at,now()),
                   legacy_payload=coalesce(legacy_payload,'{}'::jsonb)||jsonb_build_object(
                       'is_deleted','1','deleted_at',now()::text), updated_at=now()
             WHERE id=v_target_id;
        ELSIF v_target_id IS NOT NULL THEN
            DELETE FROM trosa.tasks WHERE id=v_target_id;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'reminder identity is immutable';
    END IF;
    SELECT ar.account_id INTO v_account_id FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.legacy_user_id=v_legacy_user
       AND ar.legacy_customer_id=NEW.customer_id;
    IF v_account_id IS NULL THEN RAISE EXCEPTION 'customer % is not visible for user %',NEW.customer_id,v_legacy_user; END IF;
    v_due := trosa.compat_time(NEW.remind_date);
    IF coalesce(NEW.is_done,0)=0
       AND coalesce(NEW.reminder_type,'follow_up')='follow_up' AND v_due IS NOT NULL THEN
        -- Keep direct compat-view writers serialized with the canonical writer.
        -- Both paths now acquire the date lock before allocating a legacy id,
        -- avoiding a check-then-insert race and lock-order deadlock.
        PERFORM pg_advisory_xact_lock(
            hashtext('trosa:merge-task'),
            hashtext(v_account_id::text || ':' || trosa.compat_local_date(v_due))
        );
    END IF;
    v_legacy_id:=CASE WHEN TG_OP='INSERT' AND (NEW.id IS NULL OR NEW.id=0)
      THEN trosa.compat_next_id('reminders',v_legacy_user) ELSE NEW.id END;
    SELECT lr.target_id INTO v_target_id FROM trosa.legacy_row_refs lr
     WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_legacy_user
       AND lr.table_name='reminders' AND lr.legacy_id=v_legacy_id;
    v_is_new := (v_target_id IS NULL);
    v_target_id:=coalesce(v_target_id,trosa.compat_uuid('task:'||v_legacy_user||':'||v_legacy_id::text));
    -- A new open follow-up landing on a date that already holds one merges
    -- into the earliest row instead of creating a second Today entry.
    IF v_is_new AND coalesce(NEW.is_done,0)=0
       AND coalesce(NEW.reminder_type,'follow_up')='follow_up' AND v_due IS NOT NULL THEN
        SELECT t.id, lr.legacy_id, t.title, t.content, t.reason
          INTO v_survivor_target, v_survivor_legacy,
               v_survivor_title, v_survivor_content, v_survivor_reason
          FROM trosa.tasks t
          JOIN trosa.legacy_row_refs lr ON lr.target_id=t.id
               AND lr.table_name='reminders'
               AND lr.organization_id=trosa.compat_org_id()
               AND lr.legacy_user_id=v_legacy_user
         WHERE t.account_id=v_account_id AND t.status='open' AND t.task_type='follow_up'
           AND (t.due_at AT TIME ZONE 'Asia/Shanghai')::date=(v_due AT TIME ZONE 'Asia/Shanghai')::date
         ORDER BY t.created_at ASC, t.id ASC
         LIMIT 1;
        IF v_survivor_target IS NOT NULL THEN
            SELECT string_agg(part, ' / ' ORDER BY first_seen)
              INTO v_merged_reason
              FROM (
                SELECT DISTINCT trim(unfolded.part) AS part, min(unfolded.ord) AS first_seen
                  FROM unnest(ARRAY[v_survivor_reason, coalesce(NEW.reason, '')]) WITH ORDINALITY AS unfolded(part, ord)
                 WHERE nullif(trim(unfolded.part), '') IS NOT NULL
                 GROUP BY trim(unfolded.part)
              ) merged;
            UPDATE trosa.tasks
               SET title=coalesce(nullif(trim(NEW.title), ''), v_survivor_title),
                   content=coalesce(nullif(trim(NEW.content), ''),
                                    nullif(trim(NEW.title), ''), v_survivor_content),
                   reason=coalesce(substring(v_merged_reason from 1 for 2000), ''),
                   updated_at=now()
             WHERE id=v_survivor_target;
            PERFORM trosa.compat_set_lastrowid(v_survivor_legacy);
            NEW.id := v_survivor_legacy;
            RETURN NEW;
        END IF;
    END IF;
    -- Moving an open follow-up onto an occupied date merges the same way.
    IF TG_OP='UPDATE' AND coalesce(NEW.is_done,0)=0
       AND coalesce(NEW.reminder_type,'follow_up')='follow_up' AND v_due IS NOT NULL THEN
        SELECT t.id, lr.legacy_id, t.title, t.content, t.reason
          INTO v_survivor_target, v_survivor_legacy,
               v_survivor_title, v_survivor_content, v_survivor_reason
          FROM trosa.tasks t
          JOIN trosa.legacy_row_refs lr ON lr.target_id=t.id
               AND lr.table_name='reminders'
               AND lr.organization_id=trosa.compat_org_id()
               AND lr.legacy_user_id=v_legacy_user
         WHERE t.account_id=v_account_id AND t.status='open' AND t.task_type='follow_up'
           AND (t.due_at AT TIME ZONE 'Asia/Shanghai')::date=(v_due AT TIME ZONE 'Asia/Shanghai')::date
           AND t.id<>v_target_id
         ORDER BY t.created_at ASC, t.id ASC
         LIMIT 1;
        IF v_survivor_target IS NOT NULL THEN
            SELECT string_agg(part, ' / ' ORDER BY first_seen)
              INTO v_merged_reason
              FROM (
                SELECT DISTINCT trim(unfolded.part) AS part, min(unfolded.ord) AS first_seen
                  FROM unnest(ARRAY[v_survivor_reason, coalesce(NEW.reason, '')]) WITH ORDINALITY AS unfolded(part, ord)
                 WHERE nullif(trim(unfolded.part), '') IS NOT NULL
                 GROUP BY trim(unfolded.part)
              ) merged;
            UPDATE trosa.tasks
               SET title=coalesce(nullif(trim(NEW.title), ''), v_survivor_title),
                   content=coalesce(nullif(trim(NEW.content), ''),
                                    nullif(trim(NEW.title), ''), v_survivor_content),
                   reason=coalesce(substring(v_merged_reason from 1 for 2000), ''),
                   updated_at=now()
             WHERE id=v_survivor_target;
            UPDATE trosa.tasks
               SET status='done', completed_at=coalesce(completed_at, now()),
                   legacy_payload=coalesce(legacy_payload, '{}'::jsonb)
                       || jsonb_build_object('merged_into', v_survivor_target::text,
                                             'merged_at', now()::text),
                   updated_at=now()
             WHERE id=v_target_id;
            PERFORM trosa.compat_set_lastrowid(v_survivor_legacy);
            NEW.id := v_survivor_legacy;
            RETURN NEW;
        END IF;
    END IF;
    INSERT INTO trosa.tasks(id,account_id,title,content,reason,due_at,status,task_type,source_activity_legacy_id,manual_order,completed_at,legacy_payload,updated_at)
    VALUES (v_target_id,v_account_id,coalesce(NEW.title,''),coalesce(NEW.content,''),coalesce(NEW.reason,''),
      trosa.compat_time(NEW.remind_date),CASE WHEN coalesce(NEW.is_done,0)<>0 THEN 'done' ELSE 'open' END,
      coalesce(NEW.reminder_type,'follow_up'),coalesce(NEW.source_activity_id::text,''),coalesce(NEW.manual_order,0),
      trosa.compat_time(NEW.completed_at),coalesce(to_jsonb(NEW),'{}'::jsonb),now())
    ON CONFLICT(id) DO UPDATE SET title=excluded.title,content=excluded.content,reason=excluded.reason,
      due_at=excluded.due_at,status=excluded.status,task_type=excluded.task_type,
      source_activity_legacy_id=excluded.source_activity_legacy_id,manual_order=excluded.manual_order,
      completed_at=excluded.completed_at,legacy_payload=trosa.tasks.legacy_payload||excluded.legacy_payload,updated_at=now();
    INSERT INTO trosa.legacy_row_refs(organization_id,legacy_user_id,table_name,legacy_id,target_id)
    VALUES(trosa.compat_org_id(),v_legacy_user,'reminders',v_legacy_id,v_target_id)
    ON CONFLICT(organization_id,legacy_user_id,table_name,legacy_id) DO UPDATE SET target_id=excluded.target_id;
    PERFORM trosa.compat_set_lastrowid(v_legacy_id); RETURN NEW;
END
$$;

COMMIT;
