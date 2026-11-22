-- 业务阶段从「成交/流失/留空」扩到六档，让 Sela 和 Trosa 用同一套阶段口径。
-- 只替换 trosa.customer_states.business_stage 的取值限制：
-- 默认值、NOT NULL、已有数据、business_role 和其他表都不动。Forward-only。
BEGIN;

-- 老库可能由不同版本建表，约束名不一定就是 customer_states_business_stage_check；
-- 按约束定义找到限制 business_stage 的检查约束再删，避免漏改正式库。
DO $$
DECLARE
    old_constraint text;
BEGIN
    FOR old_constraint IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'trosa.customer_states'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%business_stage%'
    LOOP
        EXECUTE format('ALTER TABLE trosa.customer_states DROP CONSTRAINT %I', old_constraint);
    END LOOP;
END $$;

ALTER TABLE trosa.customer_states
    ADD CONSTRAINT customer_states_business_stage_check
        CHECK (business_stage IN ('', '回复过', '寄过样品', '报过价', '成交', '流失'));

COMMIT;
