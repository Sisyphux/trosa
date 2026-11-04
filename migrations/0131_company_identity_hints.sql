-- 用地址 / 法人全称 / 注册号发现「不同名字、不同域名却是同一主体」的公司。
-- 这些线索来自 sela 读官网时记录的事实，规范化后存在 prospect 档案上，
-- 只用于匹配时提示人确认，不会自动合并客户。
BEGIN;

ALTER TABLE trosa.agent_prospect_profiles
    ADD COLUMN IF NOT EXISTS address_key text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS legal_name_key text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS registration_key text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS trosa_agent_prospect_profiles_identity_idx
    ON trosa.agent_prospect_profiles (organization_id, legacy_user_id, address_key, legal_name_key, registration_key)
    WHERE address_key <> '' OR legal_name_key <> '' OR registration_key <> '';

COMMIT;
