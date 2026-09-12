# Trosa 系统状态（唯一事实文档）

> 地位：本文件是 Trosa 当前架构与运行状态的**唯一参考入口**。
> 任何重大修改（架构、重要功能、删除功能、权限变化）落地时，必须同步更新本文件，
> 并在 `docs/CHANGELOG.md` 留下架构变更记录。产品层面用户可见变化仍记录在根目录
> `CHANGELOG.md`（两者分工见本文 §10）。
>
> 审计基线：2026-09-12 16:50 CST 发布后运行代码态（commit `9d7d87b`，release
> `auto-20260912085022-9d7d87b`；包含功能收敛 commit `7535b79`）。本轮已把正式运行契约、入口安全门、健康响应和开发入口
> 收敛到 PostgreSQL；公网健康响应与受控 `verify_schema` 已确认远端正在运行该契约，且
> `audit.schema_migrations` 已通过 0001–0029 全量校验。

## 1. 系统目标

Trosa 是**三人使用的外贸 CRM 工作台**。只做一件事：

> **恢复客户上下文 → 执行已确认动作 → 记录实际事实 → 按需确认下一步及日期。**

两条不可违反的业务规则：

1. **沟通记录可以没有下一步**——记录事实不强制产生待办。
2. **待办必须同时有明确动作和日期**——不允许无动作或无日期的待办。

判断所有设计的五条标准：长期主义、简洁、真正解决现实问题、AI-native、稳定并具备容错。

## 2. 当前真实架构

```text
浏览器 / 桌面窗口 / 浏览器扩展(MV3侧边栏)
        ↓ HTTPS（公网经 Cloudflare Tunnel 回源）
ECS 单机：127.0.0.1:8080（Waitress 8线程 / Flask 单进程单体）
        ├─ serve.py：唯一正式启动入口（启动前强制 PostgreSQL；importlib 以 trade_os_web 加载 app.py）
        │     └─ 同进程启动 scheduler（无独立 worker）
        ├─ app.py（约1.5万行，158条路由，无蓝图）——路由/权限/业务编排/undo
        │     └─ 业务写入唯一通道：trosa_domain.py（Interaction / Task 读写语义）
        ├─ db.py：PostgreSQL 启动迁移 + 兼容层 + 来源审计
        │     ├─ postgres_schema_contract.py：63 必需关系 + 38 必需视图的契约
        │     └─ postgres_compat.py（延迟导入）：SQLite形状SQL改写路由到PG
        ├─ 可选层：app/engine.py（模型调用/网站读取）、gmail_sync.py（只读同步）、
        │          email_verifier.py（MX+握手，不发DATA）、ical_gen.py（ICS订阅）、
        │          scheduler.py、maintenance.py、config.py
        └─ 前端：app/static（原生JS单页，无构建步骤）+ visual-v2.css
        ↓
PostgreSQL（ECS本机容器，仅监听127.0.0.1:5432）——正式唯一业务写入源
  identity（身份/组织）→ core（公司/联系人规范实体）→ trosa（现代业务模型与读模型）
  → trade_os_compat（SQLite形状兼容层，可写触发器镜像）→ audit（审计/导入账本）
  + sela（外联同步面）+ 已冻结退役面（基表保留供审计）
        ↓
/var/lib/trade-os/uploads（附件与导入来源文件本体）
```

正式运行契约为 `trosa-postgresql-v1`：`CRM_ENV=production` 只能配合
`TRADE_OS_DATA_BACKEND=postgres` 和非空 `TRADE_OS_DATABASE_URL`；
`/api/network/ping` 必须同时返回 `status=ok`、`backend=postgresql`、`formal_runtime=true`
和该契约名。
缺少配置时 `serve.py`、`init_all_dbs()` 和正式发布健康门都会失败，不会把 SQLite
当作隐式回退。SQLite 仍可用于显式隔离开发、历史演练和批准的恢复材料，但不属于
正式运行路径。

形态总结：**单进程 Flask 单体 + 单一 PostgreSQL 事实源 + 原生JS前端 + MV3采集扩展**。
没有微服务、没有消息队列、没有前端构建产物。复杂度集中在**数据兼容边界**，
不在分布式协作。模块依赖为清晰单向无环（`serve→db→contract`，
`app→{db,trosa_domain,gmail_sync,scheduler,ical_gen,config,app.engine}`，
`gmail_sync→{trosa_domain,db,app.engine}`，`trosa_domain→db`仅取postgres_mode）。

路由面实测：158 条 = 154 API（31个`/api`前缀；最大为 customers 36、integrations 21、
gateway 11、reminders 9、inbox 8、agent 8）+ 4 非API（`/`、favicon、invite、share/weekly）。

## 3. 核心模块与真实作用

| 模块 | 文件 | 真实作用 | 状态 |
|---|---|---|---|
| Web编排 | `app.py` / `serve.py` | 全部路由、权限、业务编排、undo；`serve.py` 启动前强制 PG | 在用，唯一正式入口 |
| 业务域 | `trosa_domain.py` | Interaction/Task 唯一业务读写语义；Today/Customer共用排序；外联投递历史永不转为待办（SQL强制） | 在用，唯一写入通道 |
| 数据层 | `db.py` / `postgres_compat.py` / `postgres_schema_contract.py` | PG启动迁移、兼容层、来源审计、schema契约 | 在用，关键接缝 |
| 迁移 | `migrations/0001–0029` | 6 schema沉淀；0019退役旧AI/监控兼容视图；0020建customer_states事实；0023–0029收敛兼容写边界与审计 | 在用，ledger在`audit.schema_migrations`（SHA-256） |
| 前端 | `app/static/` | 单页应用；Customer/Today/Inbox共同确认入口已发布并通过公网健康门 | 在用 |
| 采集扩展 | `browser-extension/` | MV3侧边栏，从网页邮箱/WhatsApp采集回POST `/api/extension/*` | 在用 |
| Sela同步 | `/api/integrations/sela/*` 16条 | 只同步**已确认**外联；精确身份匹配+幂等键+REVIEW；不直连DB | 在用 |
| Agent网关 | `/api/gateway/*` 11条 + `/api/agent/*` 8条 | 原子读取/确认式提案/受限幂等写入；提案与动作分离 | 在用 |
| Gmail同步 | `gmail_sync.py` + 5条集成路由 | `gmail.readonly`；仅唯一精确邮箱匹配自动写入，其余进Inbox | 可选，关闭不影响核心 |
| AI辅助 | `app/engine.py` | 沟通整理/截图识别/官网导入/问答；只预填与草稿，不直接写入 | 可选，关闭核心完整可用 |
| 日历 | `ical_gen.py` | 个人ICS订阅，只读，不回写 | 在用 |
| 调度器 | `scheduler.py` | 到期提醒、可选监控、后台任务（与Web同进程） | 在用 |
| 备份恢复 | `deploy/cloud/*.sh` + 应用undo | PG logical dump+附件bundle+SHA-256+restore-check；`undo_actions`冲突感知快照 | 在用 |
| 导入导出 | 应用内Excel链路 | 导入/导出/历史恢复格式；保留来源指纹；不可靠匹配进审阅 | 在用 |
| 已冻结 | 网站监控、AI研究/推荐、15/30/60天自动节点、Pi前端运行时 | 基表保留供审计；0019已删兼容视图；正式路径不再创建/展示/操作 | 冻结，不得恢复 |

## 4. 数据流

```text
客户/联系人 → 沟通事实（人工记录/Gmail证据/Sela已确认外联/扩展采集）
   → 明确待办（动作+日期）→ Today/日历到期呈现 → 执行 → 新事实
   ↘ 低置信/歧义 → Inbox/待审阅（人工判断后才落账）
```

- Interaction 统一呈现人工沟通、Gmail/投递事实与客户回复（`trosa.customer_interactions` + timeline）。
- Task 是唯一可执行下一步；Today/Search/Stats/Weekly 都是正式事实的视图，不是独立队列。
- `customer_facts` 是“关系与下一步”事实的唯一共享定义；各页面不得各自拼装。
- 写入统一走 `trosa_domain.py`；`POST /api/customers/<id>/follow_history` 自动完成匹配到期待办、
  建立下一待办、成功后才解决Inbox信号。Inbox“记录客户回复”复用同一确认入口，不另建写入路径。
- Excel 只是格式，不做持续双向同步；Apple日历只是ICS订阅；iCloud链路不存在。

## 5. 权限边界

- 写入绑定已认证用户，落到其 PostgreSQL 组织作用域；`TRADE_OS_DATABASE_URL` 是正式写入边界。
- 跨用户读取只返回白名单字段；三位用户（Hamid/Amy/Kelley）数据隔离。
- 客户/联系人自动确认只允许**规范化后唯一精确邮箱或手机号**；名称、昵称、公司简称、
  不完整电话只进候选/待审阅。
- Sela 用独立Bearer token（prospect/exclusion `sela-v2`，follow-up `sela-follow-up-v1`）；
  多重命中或身份冲突必须返回 `REVIEW`，不得猜测归属。
- Gateway 用个人Bearer token + scope + `Idempotency-Key`；高风险删除/批量/恢复/token管理不开放。
- **已知短板（见§8）**：运行角色疑似 superuser 且无 RLS；隔离主要依赖应用层而非数据库强制。

## 6. Agent 职责（AI/Sela/上层Agent）

- 只能做：预填来源/时间/渠道/证据/摘要/候选、去重、沟通摘要与事实提取、生成草稿与提案。
- 不能做：自动创建客户/联系人/待办、自动商业承诺、发送消息/报价/价格交期承诺（始终由人完成）。
- 无足够信息时进 Inbox/待审阅，不自动执行高风险动作。
- 技术对应：AI产物落 `audit.agent_proposals`/`agent_actions`（提案与动作分离）；
  外部同步靠 `integration_receipts` 幂等；Agent 经受限业务 API 访问，不直连 PG/SQLite/文件/Shell。
- 未配置模型时，客户、联系人、沟通、待办、Today、Inbox、Search、日历、导入导出、备份恢复**完整可用**；
  核心页面不等待 AI/网站读取/邮箱验证/后台任务，且必须区分加载/空/未登录/权限/网络/服务/解析错误。

## 7. 当前限制

- ECS 单机单进程是唯一正式运行与写入主机；无高可用、无读写分离、无多实例。
- 本地仅开发/隔离测试/已验证发布；不得修改 ECS 数据、正式备份或 `data/` 当前链接目标；
  测试必须用独立 `CRM_DB_PATH`，PG验收必须用隔离PG或只读检查。
- SQLite 仅隔离开发/导入演练/明确批准回滚；Excel 仅导入导出与历史恢复。
- `app.py` 约1.5万行、`app.js` 约8000行是已知大文件，但**不因大就拆分**（先完成完整可验证问题）。
- 新一级页面需证明现有页面/面板/动作无法承载；不新增重复概念（状态/等级/标签/复杂视图/批量管理）。

## 8. 已知问题（本轮确认）

1. **兼容层是最大风险源**：`trade_os_compat` 可写触发器与 `search_path` 共存，
   同一名称在不同上下文可能解析到不同对象；已有 `uuid=text`、`""`双引号、
   日期投影漏待办三类真实故障史。新增 SQL 必须显式限定 schema，跨层连接禁止裸`id`。
2. **共享 account 字段所有权未完全分开**：约90个 `trosa.accounts` 被多用户映射；
   0013 只标记无域名依据合并为 review，共享 payload 内备注/跟进状态仍有串扰风险。
3. **运行角色权限过大**：`tradeos_app` 疑似 SUPERUSER+BYPASSRLS，无 RLS；需独立 owner/runtime
   角色与权限矩阵演练，不可在生产库直接试改。
4. **发布链路依赖远端控制面**：Workbench 偶发超时时必须使用已配置的 SSH 回退；
   发布脚本仍要先完成备份、基线校验、同一 commit 的原子切换和健康门，不能手工跳过。
5. **ECS 正式契约与迁移账本已完成本轮验收**：当前 release 为
   `auto-20260912085022-9d7d87b`，公网四字段健康门通过，受控 `verify_schema` 确认
   0001–0029、完整 schema contract 与零 orphan reference。后续修改仍须重复该验收。

## 9. 下一阶段方向（不跳过路线图）

顺序（见 `TROSA_MAINTENANCE.md`）：保持本地回归、备份、同一 commit 发布和远端四字段
健康门；下一步再推进 Sela 证据预填与 REVIEW 交接。保持不扩张冻结功能、不新增页面与
重复概念。Agent Gateway 共享业务写入可在“不新增第二条业务路径”边界内推进。

## 10. 文档职责（唯一入口在此）

| 文档 | 地位 | 用途 |
|---|---|---|
| `docs/SYSTEM_STATE.md`（本文件） | **唯一架构事实入口** | 当前状态有争议时以此为准 |
| `docs/CHANGELOG.md` | **唯一架构变更日志** | 架构/重要功能/删除/权限变化的解释性记录 |
| `CHANGELOG.md`（根） | 产品变更日志 | 用户可感知变化 + 验证结果 |
| `AGENTS.md` | 执行宪法 | 代理开工/验证/发布铁律 |
| `TROSA_MAINTENANCE.md` | 维护路线图 | 五任务排序与可执行卡片 |
| `README.md` | 对外总览 | 启动、结构、PG rehearsal |
| `PRODUCT_DIRECTION.md` | 产品边界 | 能力优先级与数据口径 |
| `PRODUCT_DESIGN_STANDARD.md` | 设计验收标准 | 产品/文案/交互/验收 |
| `Trade OS 系统设计说明.md` | 信息模型说明 | 模型/数据流/自动化边界 |
| `使用说明.md` | 用户手册 | 启动/日常使用/恢复 |
| `DEPLOYMENT.md` + `deploy/cloud/README.md` | 运行手册 | 发布/备份/回滚 |
| `docs/architecture/*` | 带指纹的快照证据 | C4/数据/运行时拓扑与证据索引（图源为事实源，SVG为派生） |
| 其余根目录 md/docx/历史报告 | 非核心，见§11判定 | 不作为新人阅读清单 |

## 11. 历史遗留判定（本轮只判定、不删除，待下一步指令执行）

- A 保留：`AGENTS.md`、`TROSA_MAINTENANCE.md`、`README.md`、`PRODUCT_DIRECTION.md`、
  `PRODUCT_DESIGN_STANDARD.md`、`Trade OS 系统设计说明.md`、`使用说明.md`、`DEPLOYMENT.md`、
  `docs/architecture/*`、`design/TRADE_OS_UI_SYSTEM.md`。
- B 合并后归档：`GITHUB_MIGRATION.md`（并入 README/DEPLOYMENT 后归档）；
  `AGENT_API_CAPABILITY_AUDIT.md`（边界已并入本文§6，原文归档备查）。
- D 归档（停止作为现行文档引用）：`POSTGRESQL_FINAL_CUTOVER_CHECKLIST.md`（历史切换证据）、
  `report-source.md`（2026-09-07深度审计原文，结论已并入§8）、
  `Trade OS 重构审查报告.md`（自声明历史文档）、`Trade OS AI 设计原则与功能边界.docx`、
  `产品宣传册-Publimpresos-20260817.md`、`design-qa.md`（单次验收笔记）、`archive/*.tar.gz`。
- 待确认删除（本轮不执行）：`pi-agent/node_modules/`（环境产物，不应进仓）、
  `.agents/` `.codex/` `.trae/` `.workbuddy/` 空壳目录。删除需可恢复快照并经用户确认。

## 12. 事实 / 推断 / 不确定

- 已确认事实：单体形态、无蓝图158路由、单向无环依赖、trosa_domain唯一写入通道、
  PG为正式唯一源、正式运行 guard/health 契约、兼容层可写触发器机制、Sela/Gmail/ICS/AI
  边界、冻结清单、三类兼容层故障史、90个共享account计数（2026-09-07只读核验）；
  ECS 最近一次运行代码 release 为 `auto-20260912085022-9d7d87b`，公网正式契约通过，生产库
  `audit.schema_migrations` 0001–0029 全部通过，两个 legacy reference orphan count 为 0。
- 合理推断：ECS 设计目标与部署脚本均以 PG 为正式源；当前生产运行态已由上述受控证据确认，
  后续只需对新增发布重复同一验证。
- 不确定：Cloudflare Access 是否启用；`sela.*` schema 当前是否仍在写入；
  `business_stage/role/judgment` 全量取值语义；Inbox 判定规则书面化缺失；超管运行角色当前是否已降权。

## 13. 未来修改规则（铁律）

1. 修改前先读本文件，确认当前架构定义与边界。
2. 落地后同步更新本文件对应章节。
3. 落地后在 `docs/CHANGELOG.md`（架构）与根 `CHANGELOG.md`（用户可见时）分别记录。
4. 删除废弃实现，不允许新旧长期并存；退役先隐藏入口、记录使用、确认无导出/审计引用后再迁。
5. 保持单一事实来源；不新增重复概念、重复状态字段、第二条写入路径。
6. 禁止删除历史变更记录；禁止新增第三套 changelog；禁止把“可能有用”的历史文件留在核心阅读路径。
