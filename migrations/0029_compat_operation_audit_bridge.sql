-- Compatibility clients may still insert through the old operation_logs view.
-- Keep that write path safe by recording the same immutable audit fact before
-- refreshing the integer projection.  The application itself writes the
-- canonical table directly; this trigger is only the retirement boundary.

BEGIN;

CREATE OR REPLACE FUNCTION trade_os_compat.operation_logs_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    u text := trosa.compat_current_user();
    lid bigint;
    occurred timestamptz;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'canonical operation audit is immutable';
    END IF;
    IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'operation log identity is immutable';
    END IF;
    lid := CASE WHEN coalesce(NEW.id,0)<>0 THEN NEW.id
                ELSE trosa.compat_next_id('operation_logs',u) END;
    occurred := coalesce(trosa.compat_time(NEW.created_at), now());
    INSERT INTO trade_os_compat.operation_log_rows
        (legacy_user_id,id,action,target_type,target_id,details,created_at,user_id)
    VALUES
        (u,lid,coalesce(NEW.action,''),coalesce(NEW.target_type,''),NEW.target_id,
         coalesce(NEW.details,''),coalesce(NEW.created_at,''),u)
    ON CONFLICT (legacy_user_id,id) DO UPDATE SET
        action=excluded.action,target_type=excluded.target_type,target_id=excluded.target_id,
        details=excluded.details,created_at=excluded.created_at,user_id=excluded.user_id;
    INSERT INTO audit.operation_log_events
        (id,organization_id,legacy_user_id,legacy_id,action,target_type,target_id,
         target_reference,details,occurred_at)
    VALUES
        (trosa.compat_uuid('operation-log:'||u||':'||lid::text),trosa.compat_org_id(),u,lid,
         coalesce(NEW.action,''),coalesce(NEW.target_type,''),NEW.target_id,'',
         coalesce(NEW.details,''),occurred)
    ON CONFLICT (organization_id,legacy_user_id,legacy_id) DO UPDATE SET
        action=excluded.action,target_type=excluded.target_type,target_id=excluded.target_id,
        target_reference=excluded.target_reference,details=excluded.details,
        occurred_at=excluded.occurred_at;
    PERFORM trosa.compat_set_lastrowid(lid);
    RETURN NEW;
END
$$;

COMMIT;
