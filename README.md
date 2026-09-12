# Trosa

Trosa 是三人使用的外贸客户关系工作台。它围绕客户、联系人、互动记录和明确待办组织日常工作，帮助团队保存关系上下文、看见到期承诺并记录实际进展。

## 当前架构

```text
浏览器 / 桌面窗口
        ↓
Flask 应用（app.py）
        ↓
业务规则与接口
        ↓
ECS PostgreSQL（正式唯一业务写入源，127.0.0.1:5432）
        ├─ identity / core / trosa / sela / audit
        └─ 兼容层：保留旧 SQLite 形状的 API，不再创建个人 SQLite 主库
        ↓
/var/lib/trade-os/uploads/  客户附件与导入来源
```

- 核心闭环：客户与联系人 → 已确认沟通事实 / 邮件证据 → 明确待办 → 到期执行 → 新记录。
- 核心页面：今天、Inbox、客户、本周工作；完整日历、全部记录和操作日志从场景入口进入。
- 数据存储：正式 ECS 使用 PostgreSQL 作为业务数据唯一事实源；SQLite 仅用于隔离开发、历史导入/演练和明确批准的回滚材料。Excel 用于导入、导出和历史恢复。
- 数据保护：正式备份由 PostgreSQL logical dump 加客户附件 bundle 组成，并在独立位置做 SHA-256 与 restore-check；应用不会在 PostgreSQL 模式下伪造 SQLite 快照。恢复前先保存当前版本，任何时刻只允许一个写入源。
- Apple 日历：通过个人 ICS 订阅读取待办，只同步日历事件，不复制 CRM 数据库。
- AI：可关闭的按需辅助模块，用于整理、分析和问答。关闭或未配置模型时，客户、记录、待办、Inbox、日历、导入导出和备份恢复保持完整可用。
- iCloud：当前运行链路不包含 iCloud 数据库拉取、推送或冲突合并。

### 运行契约

- 正式业务只有一个启动入口：`serve.py`。它要求 `CRM_ENV=production`、
  `TRADE_OS_DATA_BACKEND=postgres` 和非空 `TRADE_OS_DATABASE_URL`，否则在迁移、
  调度器和端口启动前失败；不会把缺失配置解释成 SQLite。
- `/api/network/ping` 必须同时报告 `status=ok`、`backend=postgresql`、
  `formal_runtime=true` 和 `runtime_contract=trosa-postgresql-v1`。发布脚本以这四个字段
  作为健康门。
- `app.py`、`desktop.py` 和根目录旧启动器不属于正式入口：前者只允许显式隔离
  SQLite 开发，后两者只打开已验证的服务或明确拒绝启动。PostgreSQL 演练使用
  `serve_rehearsal.py` / `tools/postgres_rehearsal.py`。

### 正式业务模型与兼容边界

- PostgreSQL 中的 `trosa.customer_records`（Customer）、`trosa.customer_contacts`（Contact）、`trosa.customer_interactions`（Interaction）、`trosa.customer_tasks`（Task）、`trosa.inbox_items`（Inbox）是当前正式业务读模型。`trosa.customer_details` 保存 Customer 的备注、来源、外部身份和手动下一步标记；新产品代码通过 `trosa_domain.py` 读取这些模型。
- Interaction 统一呈现人工沟通、Gmail/发送投递事实和客户回复；Task 是唯一的可执行下一步，Today、Search、Stats 和 Weekly 都是这些正式事实的视图，而非独立队列。
- Sela 只保存本地 Agent 的执行、幂等、delivery journal 与恢复技术状态；它通过 Trosa Gateway 写回正式事实，绝不维护客户、联系人、阶段、任务或人工判断副本。
- `trosa.customer_states` 是 `business_stage`、`business_role` 与 `customer_judgment` 的正式 PostgreSQL 事实源；旧 `status`、`type`、`attention_*` 仅为导入、恢复和历史兼容保留。
- `follow_up_logs`、`outreach_emails`、`reminders` 和 `customers` 是旧 HTTP/SQLite 形状的 compatibility view。它们的写入会落到上面的 canonical PostgreSQL facts；它们不是新功能的业务模型。
- Today 只读取有动作和日期的开放任务；`outreach_*` 自动开发节点、网站监控和旧 AI 研究不参与正式运行路径。
- “未建立真实沟通”是客户列表筛选，不是第二个 New Customer 数据域；已发送但未获回复的开发邮件仍是邮件证据，不能被误写成客户已联系。

## 项目结构

```text
app.py                 Web 应用、接口与业务编排
serve.py               唯一正式 Waitress 入口（只接受 PostgreSQL）
serve_rehearsal.py     隔离 PostgreSQL 演练入口
db.py                  PostgreSQL 运行入口、SQLite 隔离/演练、迁移与来源审计
app/engine.py          可选的模型调用与网站内容处理
scheduler.py           到期提醒、可选监控和后台任务
ical_gen.py            个人 ICS 日历订阅
maintenance.py         存储检查与维护
email_verifier.py      可选的邮箱可发送性后台复核
app/static/            单页前端
tests/                 风险回归测试
deploy/                Linux 服务与 Tunnel 示例
design/                视觉系统与原型
archive/               历史发布包
```

## 文档职责

| 文件 | 用途 |
|---|---|
| `PRODUCT_DIRECTION.md` | 当前产品边界与能力优先级 |
| `PRODUCT_DESIGN_STANDARD.md` | 产品、文案、交互和验收规则 |
| `Trade OS 系统设计说明.md` | 信息模型、数据流和自动化边界 |
| `使用说明.md` | 启动、日常使用、数据保护和可选配置 |
| `DEPLOYMENT.md` | 家庭服务器上线与备份要求 |
| `CHANGELOG.md` | 已落地变更与验证结果 |

`Trade OS 重构审查报告.md` 和 `Trade OS AI 设计原则与功能边界.docx` 记录特定阶段的审查背景，供追溯决策使用；当前规则以上表中的现行文档为准。

## 启动与验证

日常使用请打开 `https://app.trosa.space`；Windows 可双击 `start.bat`，Mac 可双击
`Mac启动器.command`。这两个入口只打开正式 Trosa，不启动本地业务服务。

正式主机由 systemd 使用：

```bash
CRM_ENV=production \
TRADE_OS_DATA_BACKEND=postgres \
TRADE_OS_DATABASE_URL='postgresql://tradeos_app@127.0.0.1:5432/tradeos' \
.venv/bin/python serve.py
```

本地隔离 SQLite 仅在明确需要旧形状回归时使用：

```bash
CRM_ENV=development TRADE_OS_DEV_SQLITE=1 .venv/bin/python desktop.py
```

涉及 PostgreSQL schema、migration、domain write path 或 compatibility retirement
时，先运行隔离 PostgreSQL 演练。改动后至少执行：

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
.venv/bin/python -m py_compile app.py db.py scheduler.py serve.py serve_rehearsal.py
node --check app/static/app.js
(cd browser-extension && npm test)
```

### PostgreSQL rehearsal（标准验收入口）

涉及 canonical schema、migration、Trosa domain write path 或 compatibility
retirement 的改动，先在本机隔离 PostgreSQL 中真实执行。项目内的
`tools/postgres_rehearsal.py` 使用本机 PostgreSQL 17 原生命令，在
`.local/postgres-rehearsal/` 建立独立集群，只监听 `127.0.0.1:55432`；它
不会读取生产 ECS 配置，也不会连接生产数据库。

```bash
# 一条命令：新建空库 → 全量 migration → 安全 fixture → schema contract → integration test
python3 tools/postgres_rehearsal.py test

# 分步开发流程
python3 tools/postgres_rehearsal.py start
python3 tools/postgres_rehearsal.py init
python3 tools/postgres_rehearsal.py migrate
python3 tools/postgres_rehearsal.py fixture
python3 tools/postgres_rehearsal.py verify
python3 tools/postgres_rehearsal.py env       # 输出当前 shell 可复制的本地 DSN
python3 tools/postgres_rehearsal.py stop
```

`test` 会重建且只重建名为 `trosa_rehearsal` 的本地数据库；`reset` 也只作用
于这个数据库，绝不会触碰 ECS 或项目的 SQLite 数据。`migrate` 记录每个
SQL 文件的 SHA-256 到 `audit.schema_migrations`，并要求完整
`postgres_schema_contract.py` 通过。单独运行 `python3 -m unittest discover
-s tests -p 'test_postgres_rehearsal.py'` 时，如果没有明确的 loopback DSN，
测试会安全跳过；不把生产 DSN 当作测试环境。

首次在本机开发时，创建 Python 虚拟环境并安装固定依赖；浏览器扩展测试也需安装其锁定的开发依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
(cd browser-extension && npm install)
```
