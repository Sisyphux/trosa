-- 业务阶段从「成交/流失/留空」扩到六档，让 Sela 和 Trosa 用同一套阶段口径。
-- 只替换 trosa.customer_states.business_stage 的取值限制：
-- 默认值、NOT NULL、已有数据、business_role 和其他表都不动。Forward-only。
BEGIN;

ALTER TABLE trosa.customer_states
    DROP CONSTRAINT IF EXISTS customer_states_business_stage_check,
    ADD CONSTRAINT customer_states_business_stage_check
        CHECK (business_stage IN ('', '回复过', '寄过样品', '报过价', '成交', '流失'));

COMMIT;
