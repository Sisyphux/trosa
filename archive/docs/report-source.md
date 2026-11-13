# Trosa PostgreSQL 深度审计报告

**日期**：2026-09-07（Asia/Shanghai）  
**读者**：Trosa 维护者、发布和数据库运维人员  
**范围**：应用 SQL、`postgres_compat.py`、PostgreSQL 迁移/兼容视图、schema contract、部署文档，以及本轮对 ECS 正式 PostgreSQL 的只读核验。  
**边界**：本轮未写入 ECS、未修改正式备份、未发布代码；生产数据只做聚合/结构性读取，不记录客户原文或凭据。

## 直接结论

这次错误确实暴露了一个系统性边界：兼容层保留 SQLite 形状（文本 legacy ID、`?` 参数、整数布尔值），canonical PostgreSQL 层使用 UUID、typed timestamp 和真实约束。单处把兼容视图 `users.id` 与 canonical `identity.memberships.user_id` 连接，就会触发 `uuid = text`。

PostgreSQL 实例本身目前不是“整体损坏”：只读核验显示 PostgreSQL 17.11 healthy，`audit.schema_migrations` 的 001—013 记录与本地迁移 hash 一致，schema contract 完整，用户/成员状态、外键孤儿、重复用户名等当前计数为 0，备份 timer 最近一次成功。但这不能等同于“没有 PostgreSQL 风险”。当前最重要的风险是兼容边界、数据库角色权限和共享 account 的数据所有权边界。

## 已确认并已处理

1. `disable_team_member` 已改为显式连接 `identity.memberships.user_id = identity.users.id`，再用 `username/legacy_user_id` 定位用户；不再把兼容视图的文本 ID 与 UUID 直接比较。
2. 发现两处 PostgreSQL 不接受的 SQLite 双引号空字符串：客户恢复接口的 `deleted_at = ""`，以及 Agent Gateway 客户搜索的 `COALESCE(..., "")`。已改为 SQL 标准单引号，并增加回归测试。
3. 上述代码只在本地工作区，尚未发布 ECS；只读检查显示当前正式 release 仍包含这两处旧写法，因此正式环境仍可能在这两个请求路径报 PostgreSQL 语法错误。

## 深度审计发现

### 1. 高优先级：运行角色是 superuser，且没有 RLS 防线

ECS 只读权限核验显示 `tradeos_app` 同时具备 `SUPERUSER`、`CREATEROLE`、`CREATEDB`、`BYPASSRLS`，拥有应用六个 schema 及 114 个 relation；抽查的 canonical 表均未启用 RLS。当前用户隔离主要依赖 Flask 上下文、`trade_os.user` session setting 和兼容视图，而不是数据库强制边界。

这意味着一旦出现 SQL 注入、错误的后台查询或凭据泄露，攻击面不是“当前用户能看到的几行”，而是整个数据库及权限体系。PostgreSQL 官方文档也明确说明 superuser 会绕过访问限制，RLS 还会受 `BYPASSRLS` 影响。

**建议**：单独建立 schema/table owner 与 runtime login；runtime 角色去掉 superuser/createrole/createdb/bypassrls，并重新设计兼容视图 trigger 的执行权限。这个改动不能直接在当前生产库试改，需要迁移前的权限矩阵、隔离库演练和回滚窗口。

### 2. 高优先级：`search_path` 让兼容名与 canonical 名共存，容易重复同类错误

应用连接固定使用 `trade_os_compat,trosa,core,identity,audit,sela,public`，大量旧 SQL 依赖不带 schema 的 `users/customers/contacts`。PostgreSQL 会使用 `search_path` 中第一个匹配对象；因此同一个 `users` 在不同上下文可能是兼容 view、canonical table，或被临时对象遮蔽。官方文档也警告可写 schema 会影响未限定名称的解析。

**建议**：保留旧 SQL 的兼容入口，但新增静态 lint/测试：凡访问 `identity/core/trosa/sela/audit` 的 SQL 必须显式限定；跨层连接必须显式写 `legacy_user_id ↔ uuid` 的转换/映射，禁止裸 `id` 连接。逐步把真正的管理/身份 SQL 全部 schema-qualify。

### 3. 高优先级：共享 account 的“公共公司字段”和“用户私有字段”没有完全分开

正式库只读计数发现 90 个 `trosa.accounts` 被多个 legacy 用户映射。这个共享本身可能是有意的公司归并，但代码语义存在明确风险：导入器以 `company_id` 生成同一 account，并在冲突时保留先写入的 account；`trosa.customers` 又从共享 account 的 `legacy_payload` 读取 `notes`、`system_notes`、跟进日期等字段。兼容写入 trigger 还会把当前用户提交的整行 JSON 合并回共享 `legacy_payload`。

所以“当前用户过滤了 `account_legacy_refs`”并不能保证备注、跟进状态和历史 legacy payload 是用户私有的。迁移 0013 只把无明确域名依据的跨用户公司标成 `review`，没有解决共享 account 内部字段的所有权问题。

**建议**：先定义字段矩阵：公司名称/域名等可共享字段保留在 account/company；备注、系统备注、pin、跟进日期、用户自定义状态和 legacy payload 改为 user-scoped projection；再为 Hamid/Amy/Kelley 做只读差异核验和跨用户写入测试。不要直接拆现有 90 条数据或删除映射。

### 4. 中优先级：没有数据库级 statement/lock/idle transaction timeout

正式库只读 settings 显示 `statement_timeout=0`、`lock_timeout=0`、`idle_in_transaction_session_timeout=0`；Waitress 默认 8 个线程，每个请求独立建立 psycopg 连接。一个慢查询、锁等待或异常后未及时结束的事务，可能长期占住请求线程。Psycopg 文档也说明事务出错后必须 rollback，否则后续命令会继续处于 aborted transaction 状态。

**建议**：按读接口、写接口和迁移任务分别制定 timeout；先在隔离库压测，再用连接级或事务级设置落地，避免用一个全局小值破坏大查询/备份。

### 5. 中优先级：多组织约束目前主要靠代码，而非复合外键

canonical 表普遍同时保存 `organization_id` 与 `company_id/account_id/user_id`，但许多外键只约束后者。例如 account 的 `company_id`、`owner_user_id` 和 contact/company 关联没有把 organization 一并纳入约束。当前正式库只有一个 organization，不能形成现存数据错误；一旦未来启用多组织，这会允许跨组织关联被写入。

**建议**：在多组织功能真正启用前，补齐 `(organization_id, id)` 的复合唯一键和复合外键，并在 schema contract 中检查；不要用当前单组织数据直接证明该边界已经安全。

### 6. 发布验证仍缺少“当前 001—013 基线”的真实写入—回滚闭环

schema/read-only 检查和历史切换演练都很有价值，但本轮没有对当前正式 release 做写入、回滚、Sela 幂等重放、dump+附件隔离恢复的完整受控演练。维护文档也要求 PostgreSQL 关键读写不能由 SQLite 回归替代。下一次发布必须把这些作为 gate，而不是只看 migration ledger 或 `/api/health`。

## 推荐处理顺序

1. 在受控窗口发布本地两个 SQL 修复，并做客户恢复、Gateway 搜索、禁用成员三条真实 PostgreSQL 路径的写入/读取/回滚验证。
2. 先做 runtime DB role 降权设计和隔离库演练，再动正式权限；不要把当前 superuser 角色直接改名或撤权。
3. 明确共享 account 字段所有权，补 user-scoped projection 与跨用户读写回归；对现有 90 个共享 account 只做报告和人工确认。
4. 增加连接/事务 timeout、阻塞查询和 idle transaction 监控。
5. 在 schema verifier 中加入 role flags、RLS、search_path、timeout、跨组织 FK 和 user-scope smoke checks。

## 限制与证据来源

- 生产核验只读；没有用生产写入试探风险，也没有查看客户原文。
- 代码证据：`app.py` 的禁用成员、客户恢复、Gateway 查询；`postgres_compat.py` 的 SQL 翻译和 `search_path`；迁移 0001、0002、0013 的 canonical 表、兼容 view 和公司匹配边界；`serve.py` 的 Waitress 线程配置。
- 维护边界见 `TROSA_MAINTENANCE.md` 与 `POSTGRESQL_FINAL_CUTOVER_CHECKLIST.md`；本报告不改写历史切换记录。

## Claim-to-source ledger

| 结论 | 主要证据 |
|---|---|
| UUID/text 根因 | `identity.memberships.user_id`/`identity.users.id` 为 UUID；兼容 `users.id` 映射 `legacy_user_id` 文本；`app.py` 原禁用 SQL 的裸表解析 |
| 裸名解析风险 | `postgres_compat.py` 的固定 `search_path`；PostgreSQL [schema/search_path 文档](https://www.postgresql.org/docs/current/ddl-schemas.html) |
| 类型解析行为 | PostgreSQL [operator type resolution 文档](https://www.postgresql.org/docs/current/typeconv-oper.html) |
| 视图写入边界 | PostgreSQL [CREATE TRIGGER 文档](https://www.postgresql.org/docs/current/sql-createtrigger.html) |
| 事务失败后的状态 | Psycopg [transaction management 文档](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) |
| superuser/RLS 风险 | PostgreSQL [CREATE ROLE 文档](https://www.postgresql.org/docs/current/sql-createrole.html) 与 [row security 文档](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) |
| 当前生产健康证据 | ECS 只读 schema verifier、迁移 ledger、约束/孤儿/用户状态/备份 timer 检查（本轮不包含任何写入） |
