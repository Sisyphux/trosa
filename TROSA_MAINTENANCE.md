# Trosa 长期维护计划

> 更新：2026-09-12。依据当前仓库、ECS 只读运行链路、正式 PostgreSQL 运行契约和本地真实 PostgreSQL 演练整理。本文是维护入口，不是重构方案；正式发布后的远端版本与数据库账本仍以 `status-workbench.sh` 和受控备份/核验记录为准。本轮功能收敛 commit `7535b79` 已发布，当前线上 release 为 `auto-20260912082853-0a25827`（文档同步）。

## 先读这一页

Trosa 应继续做一件事：让业务员在需要时恢复客户上下文、记录已经发生的事实、确认明确且带日期的下一步，然后在到期日再次执行。它不应重新变成“维护一大堆客户状态”的 CRM，也不应把 AI 或 Sela 变成自动承诺、自动建待办的黑箱。

当前最有价值的改进不是新增页面，而是把分散在 Customer、Today、Inbox 和 Search 的同一次沟通收敛为一个可确认的入口。

## 当前运行事实

### 正式运行方式

```text
浏览器 → Cloudflare Access / Tunnel → ECS 127.0.0.1:8080
                                      ↓
                               Waitress / Flask（serve.py、app.py）
                                      ↓
                         PostgreSQL（127.0.0.1:5432，正式唯一业务写入源）
                              ↓
                    /var/lib/trade-os/uploads（附件与来源文件）
```

- 正式入口：`https://app.trosa.space`。
- 正式运行契约：`trosa-postgresql-v1`。`CRM_ENV=production` 必须同时声明 `TRADE_OS_DATA_BACKEND=postgres`、非空 `TRADE_OS_DATABASE_URL`；`serve.py`、`init_all_dbs()` 和健康门均拒绝隐式 SQLite 回退。
- 正式主机：单台阿里云 ECS；`trade-os.service` 运行 `/opt/trade-os/current/serve.py`，通过 `/etc/systemd/system/trade-os.service.d/postgres.conf` 注入 `TRADE_OS_DATA_BACKEND=postgres`、PostgreSQL DSN 和权限 600 的 `PGPASSFILE`。PostgreSQL 由 `/opt/trade-os-postgres` 的容器运行，仅监听 ECS 回环地址。
- 2026-09-12 发布后只读检查：`trade-os=active/running`、`cloudflared=active`，当前 release 为 `auto-20260912082853-0a25827`；公网 ping 返回 `status=ok`、`backend=postgresql`、`formal_runtime=true`、`runtime_contract=trosa-postgresql-v1`。受控 `verify_schema` 已确认 0001–0029 全部通过、schema contract 完整、两个 legacy reference orphan count 均为 0。
- 普通代码任务完成后默认由 `deploy/cloud/auto-publish.sh` 自动验证、提交、推送 `main`，再由 `publish-workbench.sh` 让 ECS 拉取同一 commit、健康检查并原子切换 release。当前 ECS release 以 `deploy/cloud/status-workbench.sh` 的实时输出为准；发布前仍应核对状态和待发布 commit 的关系。数据库敏感改动先备份；疑似破坏性迁移需明确确认。
- 工作区可能存在用户未提交的 `deploy/cloud/` 与 `CHANGELOG.md` 运维修改；维护产品时不得覆盖或顺手提交这些修改。

### 核心架构地图

| 范围 | 当前事实 | 修改时的边界 |
|---|---|---|
| Web 与业务编排 | `app.py` 是 Flask 路由、业务规则、权限、undo 和编排中心；`serve.py` 启动生产进程。 | 不因文件大而拆分；改动优先局限在已有端点和函数。 |
| 前端 | `app/static/index.html`、`app/static/app.js`、`style.css` / `visual-v2.css` 是单页应用。 | 核心工作流改动先做局部状态更新，避免整页或整客户对象重载。 |
| 业务数据 | PostgreSQL 的 `identity`、`core`、`trosa`、`audit` 是正式事实源；`sela` schema 仅是历史导入/兼容面，不是 Sela Agent 的本地业务库；兼容层通过用户作用域映射旧形状。 | `TRADE_OS_DATABASE_URL` 是正式写入边界；`CRM_DB_PATH` 仅用于 SQLite 隔离/导入演练/明确批准的回滚，不把 Excel 变成同步源。 |
| 关系闭环 | 客户/联系人 → 沟通事实 → 明确待办 → Today/日历 → 新事实。 | 沟通可以没有下一步；待办必须有动作和日期。 |
| 保护与恢复 | `db.py` 负责 PostgreSQL 启动迁移、兼容层和来源审计；正式备份由 PostgreSQL logical dump + 附件 bundle 完成，`undo_actions` 保存冲突感知的操作快照。 | 改写入逻辑时必须保留用户隔离、来源、操作日志、undo、外键/唯一约束与可恢复备份。 |
| Sela | 通过 Bearer token 调用受限 `/api/integrations/sela/*` 接口；prospect/exclusion 使用 `sela-v2`，follow-up 使用 `sela-follow-up-v1`，由 Trosa 在 PostgreSQL 事务中精确匹配身份并以幂等键防重。 | Sela 不直接读写 Trosa 的 PostgreSQL 或文件存储；其本地 SQLite 只保留 Agent 会话、诊断、有限传输 outbox 和 Gmail delivery journal。多重命中或身份冲突必须返回 `REVIEW`。 |

### 已冻结或不应扩张的旧能力

- **网站监控、客户 AI 研究/推荐**：历史数据保留，但已从用户模块和 Inbox 活动信号中冻结；不恢复为核心流程。
- **15/30/60 天自动开发节点**：历史行仅保留用于数据/迁移审计；正式运行不再创建、展示或操作，不要把它们重新混入人工待办。
- **旧式客户等级、阶段、状态、信息缺口、手动排序和大量动态视图**：历史兼容可保留，默认界面不应继续增加依赖；先降级为次级信息，再决定是否停止暴露。
- **开发信、批量操作、邮箱可发送性复核、本周工作和 Excel 入口**：保留可用性与数据兼容，不做下一阶段的产品扩张。是否删除前须先看真实使用和导出/审计依赖。
- **前端 Pi 聊天运行时**：已移除。保留的 `/api/agent/*` 是给上层 Agent 的原子工具和确认式提案，不应重新引入独立前端 Agent。

## 功能处置

| 决策 | 功能 | 原因与做法 |
|---|---|---|
| 保留并维护 | 客户、联系人、时间线、明确待办、Today、日历、Search、Inbox、归档/恢复、导入导出、备份 | 这是不依赖模型也能完成日常工作的闭环。任何改动先保护它。 |
| 简化 | Customer 工作区、沟通记录入口、Today 完成动作、Inbox 手工回复、搜索结果、客户筛选 | 当前把同一工作分散到多处，且旧 CRM 元数据压过“刚发生什么、现在做什么”。 |
| 冻结 | 网站监控、客户 AI 研究/推荐、自动开发节点运行时、批量经营工具继续扩张 | 这些能力已退出正式运行面；历史表、字段和迁移记录只为数据恢复与审计保留。 |
| 未来删除候选 | 默认客户列表中的等级/状态批量维护、旧开发信专属录入、未被使用的复杂视图 | **不是现在删除字段或接口。** 先隐藏默认入口、记录 60–90 天使用情况、确认没有导出/审计引用后，再做单独迁移与回滚计划。 |
| 交给 Sela / AI | 已确认外联的同步、来源/时间/渠道/身份证据预填、去重、沟通摘要和明确事实提取、低置信度匹配候选 | Sela 负责受限自动采集与已确认外联；AI 只生成草稿和候选。客户归属、下一步动作/日期、任何商业承诺始终由人确认。 |

## 按价值排序的五个任务

排序标准：实际使用价值 × 使用频率 × 降低操作负担 × 改动风险；不是按“最容易改”排序。

| 排名 | 任务 | 为什么现在做 | 价值 / 频率 / 减负 / 风险 |
|---:|---|---|---|
| 1 | 建立一个“沟通捕获 → 整理 → 确认”的共同入口 | **已发布并通过正式健康门。** Customer、Today、Inbox 共用确认面板；既有 `POST /follow_history` 仍写入事实，Inbox 仅在确认成功后解决。 | 高 / 高 / 高 / 中 |
| 2 | 将 Customer 工作区收敛为“现在”优先 | **已发布并通过正式健康门。** 首屏突出最近事实、当前等待与一个下一步动作；联系人、文件、完整时间线、资料缺口和历史分类仍可按需展开。 | 高 / 高 / 高 / 低–中 |
| 3 | 让 Inbox 与 Search 带着上下文进入共同入口 | **已发布并通过正式健康门。** Inbox 客户回复与浏览器采集带入共同确认面板；Search 提供受限 `match_context`、查看客户和记录进展续接。 | 高 / 中高 / 高 / 中 |
| 4 | 固化 Sela 的“证据同步 + 待审阅”交接 | Sela 已稳定写入已确认外联；下一步应让它预填证据和去重，而非另做 CRM 流程或自动承诺。 | 中高 / 中 / 高 / 中 |
| 5 | 保持可重复的本地回归与发布前检查 | Python 依赖现已由根目录 `.venv` + `requirements.txt` 固定，扩展依赖由 `browser-extension/package-lock.json` 固定；后续改动仍必须先通过这套保护。 | 高 / 每次维护 / 间接高 / 低 |

## 前三项的可执行卡片

### 1. 共同沟通确认入口

**当前问题**

Customer 的沟通表单、Today 的完成与记录、Inbox 的“记录客户回复”各自拥有动作。用户重复选择渠道、方向、日期、客户和下一步；同一事实也可能被记录两次。虽然 `POST /api/customers/<id>/follow_history` 已会自动完成匹配的到期待办、建立下一待办并清除相关 Inbox 信号，但它没有被所有入口作为唯一终点。

**目标状态**

一个共享确认面板接收不同入口的上下文：客户、当前待办、原始消息、渠道、方向、日期和来源。用户只确认三件事：

1. 这次实际发生了什么；
2. 是否完成当前明确待办；
3. 是否新增一条带动作和日期的待办。

Sela/扩展提供的准确来源、时间、渠道、联系人和原文只做预填与去重；AI 可提出摘要、事实和下一步候选，不能直接写入。

**页面 / 文件 / API**

- 页面：Customer 工作区、Today 右侧焦点区、Inbox “客户回复”。
- 前端：`app/static/index.html`、`app/static/app.js`；仅在需要时微调 `style.css` 或 `visual-v2.css`。
- 复用的写入 API：`POST /api/customers/<customer_id>/follow_history`（`app.py`）；`POST /api/inbox/<item_id>/record-reply` 仅处理仍在运行的客户回复条目并复用共同确认入口。
- 保护测试：`tests/test_risk_regressions.py`、`tests/test_browser_extension.py`；必要时为该共同路径新增少量端到端 API 回归。

**最小修改范围**

- 在既有 `app.js` 中提炼当前沟通表单的状态和提交动作为一个共享函数，不拆文件。
- 共同提交仍调用既有 follow-history 写入；Inbox 只传入预填上下文。
- 不新增业务表、不迁移数据库、不更改 `follow_up_logs`、`reminders` 的历史字段或 Sela 契约。
- 写入成功后仅更新受影响的 Customer、Today 和 Inbox 局部状态。

**主要风险**

- 补录历史沟通不应错误完成今天的待办；现有逻辑以沟通日期匹配到期任务，必须保持。
- 预填原文可能让用户误以为已确认；面板必须明确区分“原始证据”“AI 整理”“将要写入”。
- Inbox 旧条目的撤销、归档和去重不能丢失。

**验证**

- 在隔离 SQLite 副本中覆盖：只记录事实、记录并完成到期待办、记录并创建下一步、补录旧日期、Inbox 回复、重复提交、撤销；PostgreSQL 关键读写另须在真实/隔离 PostgreSQL 中验收。
- 确认每种路径最多新增一条沟通、一条明确待办；Today、时间线和 Inbox 的结果一致。
- 无模型配置时完整可用；AI 失败时保留人工表单。
- 回归 Sela 幂等同步、浏览器扩展精确匹配和三用户隔离。

### 2. Customer 工作区的“现在”优先层

**当前问题**

Customer 是核心恢复上下文的页面，但开场承载了等级、状态、等待、信息缺口、资料和多个浮动动作。它保留了较多旧 Trosa 的“客户管理面板”思路，用户需要先判断信息层级与按钮，再记录实际进展。

**目标状态**

打开客户时首屏只有三个固定区域：最近发生的事实、当前明确待办/等待、记录最新进展。联系人、文件、完整时间线、资料缺口和历史分类仍可按需查看，但不抢占首屏。等级和旧状态只作为可折叠的历史元信息。

**页面 / 文件 / API**

- 页面：Customer 列表打开后的客户工作区；Today 焦点区保持相同的“现在”语义。
- 前端：`app/static/index.html`、`app/static/app.js`、`app/static/style.css`、`app/static/visual-v2.css`。
- 读取 API：`GET /api/customers/<id>/summary`、`/timeline`、`/tasks`；必要时只调整它们的返回组合，不改数据模型。
- 保护测试：现有客户事实摘要、时间线、Today 焦点与分页回归。

**最小修改范围**

- 改现有工作区的展示顺序和默认折叠状态，不删除任何字段、标签页或接口。
- 将“记录沟通 / 安排下一步 / 完成”缩为同一主动作加上下文相关的次操作；主动作接入任务 1 的共同入口。
- 不更改客户筛选、批量工具和字段存储逻辑，只降低默认可见度。

**主要风险**

- 老用户仍可能依赖等级、联系人或文件的快速进入；应保留可展开入口和键盘可达性。
- 工作区弹层的高度、内部滚动和 200% 缩放容易退化；必须在宽屏、iPad、iPhone、性能模式上看真实界面。

**验证**

- 有待办、无待办、只有历史沟通、无联系人、加载失败五种状态都能在首屏理解下一步。
- 记录沟通后只刷新当前客户摘要/时间线和受影响的 Today/Inbox，不重新加载所有客户。
- 跑客户摘要、时间线、联系人、附件、归档恢复及 Today 回归；真实浏览器检查键盘、焦点、200% 缩放与性能模式。

### 3. Inbox 与 Search 的上下文续接

**当前问题**

Inbox 的理念正确：只留下需要判断的信号。但手工“记录客户回复”是一条单独录入流；Search 的预览只显示客户基本信息，不能告诉用户匹配的是哪条沟通或待办，点击后直接进入重型工作区。用户找到了信息，仍要重新决定下一步。

**目标状态**

- Inbox 仍是异常/待判断队列，不是第二个沟通录入系统；客户回复点击后以已匹配客户、原文、来源和日期打开共同确认入口。
- Search 预览显示有限且可解释的命中证据（客户、联系人、最近相关事实或待办），并提供“查看客户”和“记录进展”两种明确续接。
- 不做全库 AI 搜索；确定性搜索保持本地、快速且不依赖模型。

**页面 / 文件 / API**

- 页面：Inbox、全局 Search、Customer 工作区。
- 前端：`app/static/app.js`、`app/static/index.html`、相关 CSS。
- API：`GET /api/inbox`、`POST /api/inbox/reply`、`POST /api/inbox/<id>/record-reply`、`GET /api/customers?search=...`；必要时为搜索结果增加受限的 `match_context`，不返回整段联系人隐私或完整历史。
- 保护测试：Inbox 去重/归档/稍后处理、客户回复记录、搜索分页和跨用户隔离。

**最小修改范围**

- 先改入口和已有查询返回的少量展示字段；不建立新搜索引擎、不做全文索引重构。
- 搜索预览最多五项、每项最多一条经过截断的命中说明；保留现有回车进入客户列表的退路。
- 依赖任务 1 的共同确认入口，不重复造一个 Inbox 表单。

**主要风险**

- 搜索命中说明若来自沟通或联系人，必须字段白名单并遵守用户数据库隔离。
- 不能把“没有下一步”“等待回复”误转为到期待办；Inbox 的判断语义保持不变。

**验证**

- 验证公司名、联系人、邮件片段、待办标题四类搜索，确认预览不泄露无关客户数据。
- 验证 Inbox 中已有关联/无关联、已处理、稍后处理和 AI 不可用状态。
- 通过 Search 或 Inbox 记录事实后，确认时间线、Today、Inbox 计数和撤销结果一致。

## 依赖与执行顺序

```text
1. 共同沟通确认入口
        ├── 2. Customer “现在”优先层
        └── 3. Inbox / Search 上下文续接
                 └── 4. Sela 证据预填与 REVIEW 交接

5. 本地回归与发布前检查：在 1 开始前恢复，在每一步后执行
```

建议顺序：任务 5 的本地可重复验证环境、任务 1、任务 2 与任务 3 已完成本地验证；任务 4 必须等共同确认入口和 Inbox/Search 上下文续接稳定后再接入，避免 Sela 产生第二条业务路径。Agent API 的共享业务写入可在这一边界内推进，但不得新增第二条业务路径。

## 开工前与每次发布的最小检查

1. **先保存基线**：记录 `git status --short`，确认不触碰现有 `deploy/cloud/` 未提交运维修改。
2. **在隔离数据目录验证**：SQLite 回归设置独立 `CRM_DB_PATH`；禁止指向 ECS、正式备份或日常 `data/`。PostgreSQL 迁移/运行验收另用隔离 PostgreSQL 或正式 ECS 只读检查，不能用 SQLite 结果代替。
3. **使用项目依赖跑回归**：根目录使用 `.venv`，由 `requirements.txt` 固定 Python 依赖；浏览器扩展在 `browser-extension/` 中执行 `npm install`，由 `package-lock.json` 固定测试依赖。不要使用系统 Python 或为了让测试绿而放宽测试。
4. **最少验证集合**：核心 Python 回归、`python3 -m py_compile app.py db.py scheduler.py serve.py serve_rehearsal.py`、`node --check app/static/app.js`，以及真实浏览器中的 Customer → 沟通 → Today → Inbox → Search；涉及 PG 时再运行 `python3 tools/postgres_rehearsal.py test`。
5. **自动发布入口**：普通代码任务使用 `deploy/cloud/auto-publish.sh --message ... -- FILE...`；它会执行本地回归、发布前只读 ECS 状态、提交、推送、原子发布和公网健康检查。不得用 `git add .` 混入无关修改。
6. **数据库改动保护**：涉及 schema、迁移或导入边界时，自动入口先执行 PostgreSQL logical dump + 附件 bundle 备份；疑似破坏性 SQL 不自动执行。
7. **发布后事实检查**：健康接口、三位用户隔离、一次沟通记录、一个明确待办、Inbox 消除/保留逻辑、Sela 重放幂等性。若产品有用户可见变化，同步更新 `CHANGELOG.md`。

## 绝对不能破坏的能力

- 未配置 AI 时，客户、联系人、沟通、待办、Today、Inbox、Search、日历、导入导出和备份恢复仍完整可用。
- 写入只落到当前已认证用户在 PostgreSQL 中的组织作用域；跨用户只读接口只返回白名单字段。
- 自动身份匹配只接受规范化后的唯一精确邮箱/手机号；名称、昵称、公司简称和不完整电话只可成为候选。
- 沟通记录是事实，不必强制生成下一步；新待办必须同时有明确动作和日期。
- Sela、浏览器扩展和 Agent 只经过受限业务 API；写入可追溯、可去重、可撤销或保留审计，不得直连 PostgreSQL 或 SQLite。
- 客户历史、附件、导入来源、备份清单和恢复流程不可因界面简化而被丢弃。

## 明天直接开始

从共享业务写入开始：在隔离数据库中让 UI、现有 `/api/agent/*` 与当前 Gateway 共同复用沟通、创建待办和完成待办的事务规则；不要先改等级、筛选、表结构或部署脚本。
