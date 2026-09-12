# Trosa 系统结构总览 — 模型说明

**产出方式**：`explore`（路由）→ `system-modeler`（主场景）+ `c4model` / `graphviz`（图源基础）
**模型状态**：当前状态（current state）
**基线**：2026-09-12 16:00 CST 的发布前工作树快照，见文末指纹表
**重要**：本模型针对发布前工作树（`origin/main` 基线 `f12a614` + 收敛改动）；请以文末指纹表和
当前发布记录为准，不把快照误读成移动靶。

---

## 一句话结论

Trosa 是一个**三人使用的外贸 CRM 工作台**，形态是「**单进程 Flask 单体 + 单一 PostgreSQL 事实源**」：一个约 1.5 万行的 `app.py` 承载全部 158 条路由（实测 `register_blueprint` / `add_url_rule` 计数为 **0**，没有蓝图），一个业务域模块 `trosa_domain.py` 承载全部业务读写语义，前端是原生 JavaScript 静态资源，沟通采集靠一个 MV3 浏览器扩展，整套东西跑在一台云主机上、经 Cloudflare Tunnel 回源到本机回环地址。

**没有微服务、没有消息队列、没有独立前端构建产物**——这是一个"刻意保持小"的系统。它的复杂度不在分布式协作，而集中在**数据层的兼容边界**：保留 SQLite 形状的历史兼容面，同时让 PostgreSQL 成为唯一正式业务内核。

一个容易被忽略的加载细节：`serve.py:26-34` 用 `importlib` 把 `app.py` 以模块名 `trade_os_web` 加载，**为的是避开仓库里同名的 `app/` 包**——`app/` 是 AI 引擎所在的包，`app.py` 是 Web 入口。

## 阅读顺序

1. `trosa-business-architecture.md` — 先建立业务语言（谁在做什么、数据归谁）。不熟悉 Trosa 从这份开始。
2. `trosa-system.structurizr.dsl` — 打开 **L1 系统上下文**，确认系统边界与外部依赖。
3. 同一 DSL 文件切到 **L2 容器**，看运行单元与数据存储的划分。
4. 同一 DSL 文件切到 **L3 组件**，看模块依赖（含真实 import 边）。
5. `trosa-data-topology.dot`（或已渲染的 `trosa-data-topology.svg`）— 29 个迁移沉淀出的 6 个 schema、规范表/视图与兼容层的关系。**左→右是写入方向，每个 schema 纵向一列**。
6. `trosa-runtime-topology.dot`（或已渲染的 `trosa-runtime-topology.svg`）— 运行单元实际落在哪台机器、哪个端口、哪条发布链路。**上→下是请求流向**。
7. `trosa-system.evidence.md` — 逐条证据、置信度、复核记录与未决任务。

---

## 系统边界（L1）

**边界内**：客户、联系人、沟通时间线、待办/Today、Inbox、Search、日历（ICS 订阅）、归档恢复、导入导出、备份，以及受限的 Sela 同步接口。

**边界外**：Sela（外联代理）、Google Gmail API、AI 模型服务、收件方邮件服务器、Apple 日历、Cloudflare 边缘。

**关键边界特征**：外部系统一律**单向或只读**。Gmail 是 `gmail.readonly`；Apple 日历只订阅 ICS、不能回写；邮箱验证只做 MX + 握手、不发 `DATA`；Sela 只能同步**已确认**外联。发送消息、报价、价格与交期承诺始终由人完成——这既是产品规则，也体现在接口设计里。

## 关键容器与运行单元（L2）

| 容器 | 形态 | 关键事实 |
| --- | --- | --- |
| Web 工作台前端 | 原生 JS 静态资源 | 8,480 行，由 Flask 直接提供（`static_url_path=''`），无前端构建步骤 |
| Flask 应用进程 | 单进程单体 | 158 条路由、无蓝图；Waitress 绑定 `127.0.0.1:8080`、8 线程 |
| PostgreSQL | 业务数据单一事实源 | 6 个 schema，契约声明 63 个必需关系 + 38 个必需视图，29 个迁移文件 |
| 附件与回滚材料 | 云主机本机文件系统 | `/var/lib/trade-os` |
| 沟通采集扩展 | Chrome MV3 侧边栏 | 从网页邮箱与 WhatsApp Web 采集，回 POST 到 `/api/extension/*` |

**值得注意的结构性事实**：**后台调度器与 Web 服务器运行在同一个进程内**（`serve.py:47` 调用 `start_scheduler()`），不是独立 worker。这是有意的简化，也意味着调度任务与请求处理共享进程与内存。

## 路由面分布（实测）

158 条路由 = **154 条 API（31 个 `/api` 前缀） + 4 条非 API**（`/`、`/favicon.ico`、`/invite/<token>`、`/share/weekly`）。

| 前缀 | 条数 | 前缀 | 条数 |
| --- | --- | --- | --- |
| customers | 36 | gateway | 11 |
| integrations（Sela 16 + Gmail 5） | 21 | reminders | 9 |
| inbox | 8 | agent | 8 |
| auth / team / follow-history | 各 5 | backup | 4 |
| ai / business-exclusions / contacts / extension / outreach / overview / undo | 各 3 | agent-gateway / calendar / emails / excel / invitations / network / weekly-summary | 各 2 |
| health / logs / my-weekly-logs / preferences / stats / system / version | 各 1 | | |

## 组件依赖（L3）

模块依赖是**清晰单向、无环**的：

```
serve.py ──→ db.py ──→ postgres_schema_contract.py
    │          │
    │          └─────→ postgres_compat.py（延迟导入）
    └──→ scheduler.py ──→ email_verifier.py（延迟）
                   └───→ gmail_sync.py（延迟）
app.py ──→ { db, trosa_domain, gmail_sync, scheduler, ical_gen, config, app.engine }
gmail_sync.py ──→ { trosa_domain, db, app.engine }
trosa_domain.py ──→ db.py（仅取 postgres_mode）
```

两个值得点出的结构特征：

1. **`postgres_compat.py` 是整个后端的关键接缝**：它把 SQLite 形状的 SQL（`?` 占位符、`sqlite3.Row`）改写后路由到 PostgreSQL。这既是"单进程单体却能支持两种后端"的答案，也是兼容复杂度的主要来源。
2. **`trosa_domain.py` 是唯一业务写入通道**：`app.py` 与 `gmail_sync.py` 都经它写入，没有旁路直接写库的业务代码。业务规则因此有单一落点。

## 数据拓扑要点

**分层**：`identity`（身份/组织）→ `core`（公司/联系人/文件规范实体）→ `trosa`（现代规范业务模型与读模型视图）→ `trade_os_compat`（SQLite 形状兼容层）→ `audit`（审计与导入账本）+ `sela`（历史导入面）+ 已冻结退役面。

**最值得理解的是兼容写边界**。实测（`migrations/0023:89-96`）：`zz_customers_ref_payload_write` 是**同时挂在 `trosa.customers` 和 `trade_os_compat.customers` 两个关系上的 `INSTEAD OF INSERT OR UPDATE OR DELETE` 触发器**——也就是说，两个 schema 里的 customers 都是可写面，写入都经同一个触发器镜像进规范表。0026 把它重写为读 `to_jsonb(NEW)`，**防止历史视图把规范状态字段抹掉**。

**规范审计的写入路径已在应用代码中确认**：`app.py` 的 `_record_operation_log()` 文档写明"写入一条操作审计事实及其可选的旧客户端投影"——同一套「规范事实 + 兼容投影」模式也在审计面上复现。

**已冻结但未删除**：AI 研究、客户理解、AI 推荐、网站监控的兼容视图已在 0019 删除，基表保留供审计。`app.py` 中已搜不到 `web_monitor` / `website_monitor` / `监控` 字样，确认网站监控不在运行路径上。

## 运行与发布拓扑要点

- 云主机是**唯一正式运行环境与唯一写入主机**；公网经 `cloudflared` 回源到 `127.0.0.1:8080`。
- `serve.py` 在迁移、调度器和端口启动前强制 `trosa-postgresql-v1`；正式 ping 必须报告
  `status=ok`、`backend=postgresql`、`formal_runtime=true` 和该契约名。`app.py`/`desktop.py` 不是正式启动入口。
- 发布以 **commit SHA 为锚点**：本地门禁通过 → push `main` → 云主机从 GitHub 拉该 SHA 的压缩包 → 解包到新版本目录 → 原子切换符号链接 → 重启服务 → 本机健康检查失败则回滚，保留最近 5 个版本。
- **本地门禁相当严格**：只允许 `main`、拒绝 `git add .`、拒绝未暂存改动、提交黑名单（`data|*.db|.env*`）、对 DB 敏感文件扫描破坏性 SQL、用临时 `CRM_DB_PATH` 跑单测。
- **一个容易被误读的差异**：`publish-workbench.sh` / `publish-remote.sh` 在**本机健康检查**失败时会回滚；但 `auto-publish.sh` 在**公网健康检查**失败时明确**不自动回滚**，只报错退出。
- 备份职责已分离：PostgreSQL 备份归数据库操作层（`pg_dump` + 一次性库还原校验），应用层的 SQLite 恢复接口在 PG 模式返回 `409 managed_externally`。

---

## 证据强度

| 部分 | 强度 | 说明 |
| --- | --- | --- |
| 模块结构、路由面、导入边 | **强** | 直接来自代码与本轮实测（行数、`grep -c`、`register_blueprint` 计数） |
| 运行单元、端口、启动命令、发布步骤 | **强** | 来自 systemd 单元与部署脚本本身 |
| 数据 schema 与兼容边界 | **强** | 来自迁移 DDL、契约文件与触发器定义 |
| 正式 PostgreSQL 契约 | **强（本地代码）／未知（ECS live）** | `serve.py`/`db.py`/ping 已强制；远端 release 与 migration ledger 仍需受控核验 |
| Cloudflare Access 策略 | **未知** | 仓库只体现 Tunnel |
| Sela 内部实现 | **已确认边界** | `/Users/luoxin/Desktop/Sela` 的本地 runtime、HTTPS Gateway、有界 outbox 和 Gmail delivery journal；不保存业务事实副本 |
| 列级外键与生命周期取值 | **部分** | 等级词汇已查明；阶段/判词取值未核实 |

## 未决校验任务（按重要性）

1. **ECS 是否已运行当前 release**：本地代码已强制 `trosa-postgresql-v1`，但需以远端 ping 四字段和发布记录确认。
2. **生产库是否已应用 `0029`**：代码层已更新迁移清单与契约，但运行库 ledger/schema 只能由受控生产验证。
3. Cloudflare Access 是否启用（若未启用，应用层登录是唯一门禁）。
4. `sela.*` schema 归属：历史 importer 面是否仍有写入；当前正式 Sela runtime 不依赖它。
5. 客户"阶段"与"判词"的取值。

## 维护说明

- **图源是唯一事实源**：`.dsl` 与 `.dot` 是可维护源文件，渲染出的 SVG/PNG 是派生产物，不要反向手改派生图。
- **C4 三视图只有图源，没有派生图**：`.structurizr.dsl` 请在 Qoder 的 Structurizr DSL 查看器里看。若改为离线渲染（structurizr-cli → PlantUML → SVG），布局引擎与 Structurizr 官方查看器不同，得到的图与查看器所见不一致，因此没有把这种 SVG 作为交付物。
- **打开方式**：`.structurizr.dsl` 用 Qoder 的 Structurizr DSL 格式查看器打开（三个视图：L1/L2/L3）；两个 `.dot` 已有渲染好的 `.svg` 可直接看，也可用 Qoder 的 DOT 格式查看器打开。
- **本机没有原生 Graphviz**（`brew install graphviz` 会走 rust/librsvg 源码构建并在 `ghcr.io` 上失败）。现有 SVG 由 Graphviz 16.0.0 的 WASM 构建渲染：`npm i @viz-js/viz`，再 `viz.renderString(src, {format:'svg'})`。若已装原生 `dot`，命令是 `dot -Tsvg <file>.dot -o <file>.svg`。
- **模型会随工作树漂移**：`app.py` 建模期间就在变动。提交或发布后建议重新核对 L2 容器与路由分布，并与文末指纹表比对。

## 基线指纹（发布前核验时刻 2026-09-12 16:00 CST）

| 文件 | SHA-256 前 16 位 |
| --- | --- |
| app.py | `b0b0662abe5e4363` |
| db.py | `9fb6422c8e3f044d` |
| trosa_domain.py | `6d07513e1e64bd0f` |
| postgres_schema_contract.py | `ec60cd9f79a2e172` |
| postgres_compat.py | `1d6ed2a1ddf9d5a9` |
| serve.py | `24a59d8d17a0dd7a` |
| gmail_sync.py | `4eea3ce85372e85d` |
| email_verifier.py | `79872029e6d75a42` |
| scheduler.py | `b5f2159b2fec38bb` |
| app/engine.py | `27604cdbc7f6d613` |
| app/static/app.js | `5d8291ea77b8316d` |

若某文件当前哈希与之不同，说明模型在该文件上已过期，相关结论需重新核对。

图源与派生图（改图后应同步重渲染，并更新下表）：

| 文件 | SHA-256 前 16 位 | 说明 |
| --- | --- | --- |
| trosa-system.structurizr.dsl | `65dd2e8b5b72e908` | L1/L2/L3 图源 |
| trosa-data-topology.dot | `6e2359cdfeb40fe8` | 图源（rankdir=LR） |
| trosa-data-topology.svg | `bc9178f3a4ff2eeb` | 派生，Graphviz 16.0.0 WASM |
| trosa-runtime-topology.dot | `2ca0daa4d3ef895e` | 图源（rankdir=TB） |
| trosa-runtime-topology.svg | `749e6b4cabf8ed49` | 派生，Graphviz 16.0.0 WASM |

## 下一步可按需路由

| 如果接下来要问 | 应使用的场景技能 |
| --- | --- |
| 「沟通捕获 → 整理 → 人工确认 → 待办/Today」逐步怎么走 | `flow-visualizer` |
| 「这次改动会影响哪些模块、数据与回归路径」 | `dependency-impact-analyzer` |
| 「发布链路、云主机运维、环境隔离是否可靠」 | `deployment-topology-analyzer` |
| 「兼容层该怎么收敛、何时移除」 | `evolution-planner` |
| 「这个架构有什么风险、测试覆盖够不够」 | `risk-quality-reviewer` |
| 「这些图是否还和代码一致」 | `architecture-health` |
