-- Customer transfer must move attachments too.
--
-- trosa.transfer_customer_account (0036) re-keyed the customer, contacts,
-- tasks, interactions, outreach and inbox rows but left
-- trade_os_compat.customer_file_rows on the previous owner.  The attachment
-- list reads that compatibility row through the current user, so after a
-- transfer the new owner saw the file name with an empty id (no preview,
-- download or delete) while the previous owner had already lost the customer.
--
-- 1. Replace the transfer function so it also re-keys the transferred
--    account's attachment rows.  Everything else in the function is the 0036
--    definition unchanged.
-- 2. Repair attachments of customers that were already transferred before
--    this migration: an attachment row whose owner no longer matches the only
--    legacy reference of its account is re-keyed to that reference.  Accounts
--    with several legacy aliases are left alone (ambiguous).  The repair is a
--    no-op once rows are consistent, so replaying this migration is safe.
BEGIN;

CREATE OR REPLACE FUNCTION trosa.transfer_customer_account(p_customer_id bigint, p_to_user text)
RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_from_user text := trosa.compat_current_user();
    v_from_uid uuid;
    v_to_uid uuid;
    v_account uuid;
    v_company uuid;
    v_owner uuid;
    v_new_customer bigint;
    v_new_contact bigint;
    v_new_file bigint;
    v_new_row bigint;
    v_ref_count int;
    r record;
BEGIN
    IF p_to_user IS NULL OR p_to_user = v_from_user THEN
        RAISE EXCEPTION 'target user must differ from the current owner';
    END IF;
    SELECT usr.id INTO v_from_uid FROM identity.users usr
     WHERE usr.organization_id=trosa.compat_org_id() AND usr.legacy_user_id=v_from_user;
    SELECT usr.id INTO v_to_uid FROM identity.users usr
     WHERE usr.organization_id=trosa.compat_org_id() AND usr.legacy_user_id=p_to_user;
    IF v_to_uid IS NULL THEN
        RAISE EXCEPTION 'target user % does not exist', p_to_user;
    END IF;
    SELECT ar.account_id,a.company_id,a.owner_user_id
      INTO v_account,v_company,v_owner
      FROM trosa.account_legacy_refs ar
      JOIN trosa.accounts a ON a.id=ar.account_id
     WHERE ar.organization_id=trosa.compat_org_id()
       AND ar.legacy_user_id=v_from_user
       AND ar.legacy_customer_id=p_customer_id;
    IF v_account IS NULL THEN
        RAISE EXCEPTION 'customer % is not visible to user %', p_customer_id, v_from_user;
    END IF;
    IF v_owner IS DISTINCT FROM v_from_uid THEN
        RAISE EXCEPTION 'user % does not own customer %', v_from_user, p_customer_id;
    END IF;
    SELECT count(*) INTO v_ref_count FROM trosa.account_legacy_refs ar
     WHERE ar.organization_id=trosa.compat_org_id() AND ar.account_id=v_account
       AND ar.legacy_user_id=v_from_user;
    IF v_ref_count <> 1 THEN
        RAISE EXCEPTION 'customer % has % merged aliases; resolve them before transfer',
            p_customer_id, v_ref_count;
    END IF;
    IF EXISTS (SELECT 1 FROM trosa.accounts a2
                WHERE a2.organization_id=trosa.compat_org_id() AND a2.company_id=v_company
                  AND a2.owner_user_id=v_to_uid AND a2.id<>v_account) THEN
        RAISE EXCEPTION 'target user % already owns a customer for this company', p_to_user;
    END IF;

    -- Ownership first so the reference re-keys pass the owner guard.
    UPDATE trosa.accounts SET owner_user_id=v_to_uid, updated_at=now() WHERE id=v_account;

    -- The projection and its state row move together.
    EXECUTE 'SET CONSTRAINTS customer_states_organization_id_legacy_user_id_legacy_cust_fkey DEFERRED';

    v_new_customer := trosa.compat_next_id('customers', p_to_user);
    -- Move the projection first: customer_states has a composite foreign key
    -- back to (organization_id, legacy_user_id, legacy_customer_id).
    UPDATE trosa.account_legacy_refs
       SET legacy_user_id=p_to_user, legacy_customer_id=v_new_customer,
           source_db=coalesce(source_db,'')||'+transfer'
     WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
       AND legacy_customer_id=p_customer_id;
    UPDATE trosa.customer_states
       SET legacy_user_id=p_to_user, legacy_customer_id=v_new_customer
     WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
       AND legacy_customer_id=p_customer_id;

    FOR r IN SELECT cr.legacy_contact_id FROM trosa.contact_legacy_refs cr
              WHERE cr.organization_id=trosa.compat_org_id() AND cr.legacy_user_id=v_from_user
                AND cr.legacy_customer_id=p_customer_id
              ORDER BY cr.legacy_contact_id
    LOOP
        v_new_contact := trosa.compat_next_id('contacts', p_to_user);
        UPDATE trosa.contact_legacy_refs
           SET legacy_user_id=p_to_user, legacy_contact_id=v_new_contact, legacy_customer_id=v_new_customer
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
           AND legacy_contact_id=r.legacy_contact_id;
    END LOOP;

    -- Attachments: re-key the compatibility row so the new owner can address
    -- the file by its integer id and the previous owner no longer can.  The
    -- canonical core.file_objects / core.entity_files rows are account-scoped
    -- and follow the account ownership on their own.
    FOR r IN
        SELECT f.id FROM trade_os_compat.customer_file_rows f
         WHERE f.legacy_user_id=v_from_user AND f.account_id=v_account
         ORDER BY f.id
    LOOP
        v_new_file := (SELECT coalesce(max(f2.id),0)+1
                         FROM trade_os_compat.customer_file_rows f2
                        WHERE f2.legacy_user_id=p_to_user);
        UPDATE trade_os_compat.customer_file_rows
           SET legacy_user_id=p_to_user, id=v_new_file, customer_id=v_new_customer
         WHERE legacy_user_id=v_from_user AND id=r.id;
    END LOOP;

    FOR r IN
        SELECT lr.table_name, lr.legacy_id FROM trosa.legacy_row_refs lr
         WHERE lr.organization_id=trosa.compat_org_id() AND lr.legacy_user_id=v_from_user
           AND lr.target_id IN (
               SELECT id FROM trosa.tasks WHERE account_id=v_account
               UNION ALL SELECT id FROM trosa.timeline_events WHERE account_id=v_account
               UNION ALL SELECT id FROM trosa.outreach_messages WHERE account_id=v_account
               UNION ALL SELECT id FROM trosa.inbox_items WHERE account_id=v_account)
         ORDER BY lr.table_name, lr.legacy_id
    LOOP
        v_new_row := trosa.compat_next_id(r.table_name, p_to_user);
        UPDATE trosa.legacy_row_refs
           SET legacy_user_id=p_to_user, legacy_id=v_new_row
         WHERE organization_id=trosa.compat_org_id() AND legacy_user_id=v_from_user
           AND table_name=r.table_name AND legacy_id=r.legacy_id;
    END LOOP;

    RETURN v_new_customer;
END
$$;

DO $$
DECLARE
    r record;
    v_new_file bigint;
BEGIN
    FOR r IN
        SELECT f.legacy_user_id AS from_user, f.id AS file_id,
               ar.legacy_user_id AS to_user, ar.legacy_customer_id AS to_customer
          FROM trade_os_compat.customer_file_rows f
          JOIN trosa.account_legacy_refs ar ON ar.account_id=f.account_id
         WHERE f.account_id IS NOT NULL
           AND f.legacy_user_id <> ar.legacy_user_id
           AND (SELECT count(*) FROM trosa.account_legacy_refs x
                 WHERE x.account_id=f.account_id) = 1
         ORDER BY f.legacy_user_id, f.id
    LOOP
        v_new_file := (SELECT coalesce(max(f2.id),0)+1
                         FROM trade_os_compat.customer_file_rows f2
                        WHERE f2.legacy_user_id=r.to_user);
        UPDATE trade_os_compat.customer_file_rows
           SET legacy_user_id=r.to_user, id=v_new_file, customer_id=r.to_customer
         WHERE legacy_user_id=r.from_user AND id=r.file_id;
    END LOOP;
END
$$;

COMMIT;
