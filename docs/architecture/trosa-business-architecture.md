# Trosa 业务架构理解（业务语言）

场景技能：`system-modeler` ｜ 基础技能：`c4model`、`graphviz`
商业应用适配：本仓库命中 `business-application-fitness` 触发条件（真实业务产品、三人团队日常使用、有 `AGENTS.md` 指令文件、有业务实体与迁移历史），因此本文件先讲业务，再讲技术。

---

## 1. 这个产品在做什么

Trosa 是**三人使用的外贸 CRM 工作台**。它的核心闭环是一句话：

> **恢复客户上下文 → 执行已确认动作 → 记录实际事实 → 按需确认下一步及日期。**

这句闭环本身就是产品设计原则，落在两条业务规则上（`AGENTS.md`）：

- **沟通记录可以没有下一步**——记录事实不强制产生待办。
- **待办必须同时有明确动作和日期**——不允许存在"没有动作的待办"或"没有日期的待办"。

这两条规则决定了整个数据模型的重心：**沟通时间线是事实层，待办是承诺层，两者不强制一对一**。

## 2. 参与者

| 参与者 | 在系统里做什么 | 证据 |
| --- | --- | --- |
| 销售用户（3 人） | 在客户工作区恢复上下文、记录沟通、执行已确认动作、确认下一步及日期 | `db.py:29-34` 用户注册表；`AGENTS.md` |
| 开发兼运维（同一团队） | 发布、备份、回滚；也是唯一改代码的人 | `deploy/cloud/auto-publish.sh` 单人发布链路 |

团队规模是关键约束：**没有角色/权限矩阵的需求**，只有"写入绑定已认证用户 + 跨用户读取只返回白名单字段"这一条边界。

## 3. 业务实体

| 业务对象 | 规范落点 | 关键业务规则 |
| --- | --- | --- |
| 客户 | `trosa.accounts` + `trosa.customer_details` + `trosa.customer_states` | 软删除（`set_customer_deleted:685`），有阶段/等级/判词/优先级/置顶（`set_customer_stage:897`、`set_customer_level:921`、`set_customer_judgment:461`、`update_customer_priority:706`）。等级词汇收窄且由数据库强制：`A, A+, A-, B, B+, B-, C, C+, C-, D, D+, D-`（`db.py:992`，`db.py:1746` 拼进 SQL 允许值列表） |
| 联系人 | `core.people` + `core.contact_methods` + `trosa.customer_contacts` | 匹配规则保守：只有规范化后的**唯一精确**邮箱或手机号才自动确认，其余只能进候选/待审阅（`AGENTS.md`） |
| 沟通记录 | `trosa.timeline_events` + `trosa.customer_interactions` | 单一时间线，带 `kind` + `source`；外部系统经 `record_external_interaction:151` 这一个适配入口写入 |
| 待办 | `trosa.tasks` + `trosa.customer_tasks` / `trosa.today_tasks` | **`merge_open_task:231` 按到期日合并**——同一到期日只保留一个任务，而不是堆积多条；完成走 `complete_task:331` |
| Inbox / 待审阅 | `trosa.inbox_items` | 有独立生命周期：`create_inbox_item:550` → `assign_inbox_customer:525` → `set_inbox_status:508` / `resolve_inbox_item:482` |
| 外联投递历史 | `trosa.outreach_messages` | **投递历史永不作为下一步**（见下） |
| 文件附件 | `core.file_objects` + `core.entity_files` | 0027 起附件元数据归规范表，兼容层只保留旧 HTTP 适配字段 |

## 4. 三条有代码证据的核心业务规则

### 4.1 外联投递历史不能变成待办

`trosa_domain.py:29-34` 的函数文档写得很直白：

> *"Retired outreach scheduler rows are delivery history, not tasks. They remain accessible through history/recovery but cannot become a next step."*

并且这个约束是**在 SQL 里强制**的，不只是注释——`customer_tasks` 的 SQLite 分支带 `AND COALESCE(r.reminder_type, 'follow_up') NOT LIKE 'outreach_%'`（`trosa_domain.py:56`）。这意味着历史上由调度器生成的 15/30/60 天自动开发节点，只保留为可查的历史投递记录，不会污染今天的待办列表。

### 4.2 Today 与客户工作区共用同一套排序

`customer_tasks:29` 的文档说明它是"Today 和 Customer 共用的唯一排序"（*the one ordering used by Today and Customer*）。排序键是：未完成优先 → 到期日升序 → 手工顺序 → id。**这是一个刻意消除"同一件事在两个页面顺序不同"的设计**。

### 4.3 客户事实有单一共享定义

`customer_facts:1187` 是"关系与下一步工作"事实的**唯一共享定义**：联系状态、最近沟通、下一个任务、是否在等回复。任何页面要展示"这个客户现在什么情况"，都应该走这个投影，而不是各自拼装。

## 5. 数据所有权与生命周期

**所有权边界（强）**：业务数据的单一事实源是云主机本机 PostgreSQL。SQLite 只用于隔离开发、导入导出和**明确批准的历史恢复边界**。Excel 只用于导入、导出和历史恢复。Apple 日历只订阅 ICS、**不能回写**。

**生命周期机制**：
- 客户是**软删除**（`set_customer_deleted`），不是物理删除。
- 客户阶段/等级/判词存在 `trosa.customer_states`（0020 建立）。0026 专门修了一个缺陷：让兼容层写入读 `to_jsonb(NEW)`，**防止旧的 SQLite 形状视图把规范状态字段覆盖掉**。
- 归档恢复、导入导出、备份恢复都必须保留来源、校验与审计；删除必须有明确目标、确认与可恢复快照（`AGENTS.md`）。

**审计与回滚**：`audit` schema 承担全部审计职责——`legacy_records`（导入前逐行归档）、`undo_snapshots`（撤销快照）、`agent_proposals` / `agent_actions`（AI 提案与实际动作分离）、`integration_receipts` 与 `agent_gateway_idempotency`（幂等键）、`operation_log_events`（0028 新增）。正式 PostgreSQL runtime 由 `trosa-postgresql-v1` 契约守门；生产库是否已应用最新迁移只以远端 ledger/schema 校验为准。

## 6. AI 与 Sela 的业务边界

这是本产品**最明确的一条红线**（`AGENTS.md`）：

> AI/Sela 应预填来源、时间、渠道、证据、摘要和候选，优先减少手工操作；**不能自动创建客户、联系人、待办或商业承诺**。没有足够信息时宁可进入 Inbox/待审阅，也不要自动执行高风险业务动作。**发送消息、报价、价格/交期承诺始终由人完成。**

这个边界在代码里有对应结构：AI 产物落 `audit.agent_proposals` / `agent_actions`（提案与动作分离），外部同步靠 `integration_receipts` 幂等，歧义进 REVIEW。Gmail 同步是 `gmail.readonly`，且不自动建客户或联系人。邮箱验证只做 MX + 握手、不发 `DATA`。

## 7. 已冻结与退役的业务面

`migrations/0019` 删除了 AI 研究、客户理解、AI 推荐、网站监控的兼容视图，基表保留供审计；`AGENTS.md` 明确**网站监控与客户 AI 研究/推荐不再是活动用户模块**，15/30/60 天自动开发节点的历史行**只保留用于数据/迁移审计**。产品上，它们不再是前台工作负担。

理解这一点对读数据拓扑很重要：看到 `research_reports`、`ai_recommendations`、`web_monitor_observations` 这些表，**不代表它们是活功能**。

## 8. Agent 指令覆盖说明

`AGENTS.md` 对本仓库的业务上下文覆盖**良好**，这是它比一般项目强的地方：

**已说明的内容**：产品核心闭环与两条业务规则；ECS 是唯一运行环境与唯一写入主机；本地不得修改正式数据；核心模块清单与"未配置 AI 时核心必须完整可用"；Sela 接口与幂等契约；已冻结功能清单；修改原则（不因文件大就拆文件）；验证命令。

**未覆盖或覆盖较弱的内容**：
- **逐步的权限矩阵**：只说明"写入绑定已认证用户、跨用户读取只返回白名单字段"，未列出哪些路由返回哪些字段。本轮也没有逐路由追踪授权执行点。
- **客户状态取值的业务含义**：`business_stage` / `business_role` / `customer_judgment` 有哪些取值、各自什么含义，指令文件与文档都没写。
- **Inbox 的判定规则**：什么情况进 Inbox、什么情况直接进时间线，没有书面规则。
- **数据保留期限**：附件与审计行的保留策略未在指令文件中说明（代码里有备份保留策略，但那是另一件事）。

## 9. 业务语言下的未知与待验证

| # | 未知 | 为什么会影响业务理解 |
| --- | --- | --- |
| 1 | **ECS 是否运行当前 PostgreSQL runtime 与最新迁移** | 本地代码已由 `trosa-postgresql-v1` guard、ping 和发布健康门强制；远端 release、`audit.schema_migrations` 是否已到 `0029`，只能通过受控远端检查确认 |
| 2 | 客户"阶段"与"判词"的取值 | **等级已查明**：`db.py:992` 定义 `CUSTOMER_LEVEL_VALUES = ('A','A+','A-','B','B+','B-','C','C+','C-','D','D+','D-')`，并在 `db.py:1746` 拼进 SQL 允许值列表，由数据库约束强制。阶段（`set_customer_stage:897`）与判词（`set_customer_judgment:461`）的取值仍未核实 |
| 3 | Inbox 到时间线/待办的判定规则 | 决定"什么需要人工确认"这一核心体验，目前只能从接口反推 |
| 4 | 跨用户可见性的实际执行点 | 影响多人协作时"谁能看到哪个客户"的预期 |
| 5 | `sela.*` schema 是否仍在写入 | 影响历史 importer 面是否被误当作正式运行内核；当前正式 Sela runtime 只经受限 HTTPS 接口 |

## 10. 一句话业务评价

这个仓库的**结构复杂度不高，但语义密度很高**：真正值钱的不是那 158 条路由，而是 `trosa_domain.py` 里那些被严格遵守的业务规则（投递历史不是待办、同到期日合并任务、事实有单一投影、AI 只能提案不能承诺）。**改动这个系统时最该小心的不是代码行数，而是这些规则**——它们有的写在文档里，有的只写在函数文档和 SQL 过滤条件里。
