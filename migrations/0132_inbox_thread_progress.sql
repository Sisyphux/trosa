-- Inbox 对话里 sela 的“正在做什么”：一行临时状态，每个对话最多一行，
-- 30 秒不续就过期（读取时判断）。它不是业务记录：不进时间线、不写审计、
-- 不改对话 revision。UNLOGGED：数据库重启丢了也无妨，sela 很快会重发。
BEGIN;

CREATE UNLOGGED TABLE IF NOT EXISTS trosa.inbox_thread_progress (
  thread_id uuid PRIMARY KEY REFERENCES trosa.inbox_threads(id) ON DELETE CASCADE,
  run_id text NOT NULL DEFAULT '',
  seq integer NOT NULL DEFAULT 0,
  text text NOT NULL,
  marks jsonb NOT NULL DEFAULT '[]'::jsonb,
  at timestamptz NOT NULL DEFAULT now()
);

COMMIT;
