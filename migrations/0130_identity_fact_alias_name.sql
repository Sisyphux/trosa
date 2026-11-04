-- 同一主体的又名（另一个公司名 / 品牌名）。
-- 人工在 Inbox 确认「是同一主体」后，新品牌名作为 name 类身份事实记到同一客户上，
-- 之后 Sela 再遇到这个名字直接归到该客户。只放宽 identifier_type 的取值范围，不改数据。
BEGIN;

ALTER TABLE trosa.identity_link_facts
    DROP CONSTRAINT IF EXISTS identity_link_facts_identifier_type_check;
ALTER TABLE trosa.identity_link_facts
    ADD CONSTRAINT identity_link_facts_identifier_type_check
    CHECK(identifier_type IN ('email', 'domain', 'thread', 'source', 'name'));

COMMIT;
