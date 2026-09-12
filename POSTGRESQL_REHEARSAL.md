# Trosa PostgreSQL 隔离演练

本 runbook 用于在本机验证 PostgreSQL 迁移、规范模型和 Trosa 写入路径。它只使用
`.local/postgres-rehearsal/` 下的独立 loopback 集群与固定数据库
`trosa_rehearsal`，不读取或修改 ECS、正式 PostgreSQL、正式附件或 Sela 运行状态。

正式业务路径只有 ECS `trade-os.service → serve.py → PostgreSQL`；本演练不能替代
正式服务，也不能与任何正式 writer 并行。

## 0. 硬性边界

- PostgreSQL 只绑定 `127.0.0.1:55432`；不使用生产 DSN，不打开防火墙端口。
- 演练工具只会操作固定的 `trosa_rehearsal` 数据库；不会自动删除不完整的数据目录。
- 演练输入必须是明确的只读副本。不要把正在写入的 SQLite、Sela 数据目录或其 WAL
  文件直接作为输入。
- 演练失败只允许重建本地演练库；正式数据库、附件和 Sela 技术状态不是回滚目标。

## 1. 标准验收命令

安装本机 PostgreSQL 17 后，从 Trosa 仓库根目录运行：

```bash
python3 tools/postgres_rehearsal.py test
```

该命令会：

1. 启动或创建独立本机集群；
2. 重建固定的 `trosa_rehearsal` 数据库；
3. 应用 `migrations/` 中全部迁移并校验 `audit.schema_migrations`；
4. 载入明确标记的确定性 Customer/Contact/Interaction/Task/Inbox fixture；
5. 验证 `postgres_schema_contract.py`；
6. 运行 `tests/test_postgres_rehearsal.py` 的真实 PostgreSQL 集成测试。

涉及 canonical schema、migration、`trosa_domain.py` 写入语义或兼容层退役的改动，
必须先通过这条命令。

## 2. 分步开发流程

```bash
python3 tools/postgres_rehearsal.py start
python3 tools/postgres_rehearsal.py init
python3 tools/postgres_rehearsal.py migrate
python3 tools/postgres_rehearsal.py fixture
python3 tools/postgres_rehearsal.py verify
python3 tools/postgres_rehearsal.py env
python3 tools/postgres_rehearsal.py stop
```

`reset` 只重建本机固定的 `trosa_rehearsal`：

```bash
python3 tools/postgres_rehearsal.py reset
```

若本机没有 `initdb`、`pg_ctl` 或 `pg_isready`，应记录为“未执行真实 PG 演练”，不
把 SQLite 回归结果冒充 PostgreSQL 验证；可按提示安装 `postgresql@17` 后重试。

## 3. 与正式运行契约的关系

演练 shell 会临时设置 `TRADE_OS_DATA_BACKEND=postgres`、loopback DSN 和
`TROSA_REHEARSAL=1`，变量只存在于当前命令进程。它不改 `.env`、systemd unit、
Cloudflare Tunnel 或 Sela Keychain。

正式服务仍必须通过：

```text
CRM_ENV=production
TRADE_OS_DATA_BACKEND=postgres
TRADE_OS_DATABASE_URL=<非空 PostgreSQL DSN>
```

并由 `/api/network/ping` 报告 `status=ok`、`backend=postgresql`、
`runtime_contract=trosa-postgresql-v1`。演练通过不代表 ECS 已应用当前代码或迁移；
线上状态仍需通过远端健康检查和发布记录确认。

## 4. 历史输入导入（可选、非正式运行）

若任务明确要求复核历史迁移，可使用停止后的、不可变的 Trosa SQLite 快照和旧 Sela
快照作为 importer 输入。它们只是 `audit`/迁移来源，不是当前业务 runtime，也不应
复制成 Sela 的新业务账本。

历史 Trosa 输入必须包含 `system.db` 以及其中登记的全部用户数据库；导入器应保留
源文件哈希、原始 payload、来源库/表/行号和未决匹配。旧 Sela JSON/SQLite 只可作为
审计证据读入 `audit`，不得让其重新成为候选、联系人、任务或人工 review 的事实源。

任何不确定的公司、联系人或事件匹配都必须保留为待审阅问题；不得静默合并、丢弃或
自动创建正式业务事实。历史导入不改变当前正式 Trosa/Sela 路由边界。

## 5. 验收与回滚记录

每次演练至少保留：迁移输出、schema verification、测试结果、fixture 标识和运行
时间。若使用历史输入，再追加不可变 source manifest、源哈希、计数核对、未决问题和
附件哈希核对。

回滚范围只限本机演练数据库和演练集群；保留输出用于诊断。正式恢复必须使用
PostgreSQL logical dump + 客户附件 bundle + restore-check，并遵循
`POSTGRESQL_PRODUCTION_CUTOVER.md` 的单 writer 规则。
