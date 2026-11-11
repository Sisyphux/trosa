# Trosa：Codex 长期开发速读

## 产品与当前路线

Trosa 是目前四人使用（设计上不限于四人，可按需增加使用者）的外贸 CRM，定位是“外贸业务员的客户记忆和成交线索本”：系统负责记住、算出、预填，人负责判断、确认、说话（详见 [`PRODUCT_DIRECTION.md`](PRODUCT_DIRECTION.md)，依据见 [`docs/research/`](docs/research/)）。核心闭环是：沟通进来 → 系统整理、人确认成记录 → 系统每天从事实算出跟进建议 → 人确认后才成为待办 → 到期再出现。沟通记录可以没有下一步，业务员不必在记录时就定下次跟进日期；跟进建议不是承诺，不形成逾期、不确认即过期；待办必须同时有明确动作和日期。

当前维护路线图是 [`TROSA_MAINTENANCE.md`](TROSA_MAINTENANCE.md)。Customer、Today、Inbox 的“沟通捕获 → 整理 → 人工确认”入口，以及 Customer 工作区的“现在 / 下一步”首屏收敛，已在本地完成并通过回归。普通代码任务在独立 worktree 中完成逻辑完整的 commit；验证通过并得到用户同意后，用 `agent-worktree.sh publish --task <id>`（底层是 `deploy/cloud/auto-publish.sh`）构建干净候选并发布正式服务器，发布不读取主工作区的 dirty 改动。下一阶段产品优先级是每日跟进建议，按 [`docs/research/2026-10-改造落地路线.md`](docs/research/2026-10-改造落地路线.md) 的任务清单依次推进（从 `followup-scan-rules` 开始）；不要跳过路线图直接扩张其他页面。

## 改代码到上线（唯一流程）

完整说明见 [`MULTI_AGENT_WORKFLOW.md`](MULTI_AGENT_WORKFLOW.md) §0，日常只用这几步：

1. **开工**：`deploy/cloud/agent-worktree.sh start --task <id> --owner claude --goal ... --scope ...`，在 `trosa-worktrees/<id>`（分支 `agent/<id>`）里改；主工作区有无法归属的在途改动时用 `adopt --task <id>`，不要直接 commit、覆盖或删除。只改文档/规则文件的小改动可直接在主工作区提交。
2. **提交**：写一句白话说明即可，不强制 `[<id>]` 前缀。每个任务只放自己的改动，不碰别的任务留下的未提交文件。
3. **验证**：`sync --task <id>`（变基到最新 main）后 `test --task <id>`。门禁绿只是“开发完成”，界面问题以真实浏览器里的表现为准。
4. **红区**（迁移、权限、备份、发布脚本、Sela 接口）上线前给用户一段白话摘要：影响谁、怎么发现问题、怎么撤销。
5. **上线**：`publish --task <id>`（只接受 commit/branch，会推送 GitHub 并部署服务器）。推送远程、改线上必须先得到用户明确同意；同意后由我执行，不让用户手动跑。完成 = `status=landed`（已发布且健康），用 `evidence --task <id>` 核对，不得凭“测试通过”声称已修复。
6. **收尾**：`remove --task <id> --delete-branch`（只会安全删除已发布或已等价合入的分支）。

已取消：开发/发布角色拆分（不必设 `TRADE_OS_AGENT_ROLE`）、`guard` 开工闸门、开发会话不得发布、会话租约（默认关闭，设 `TRADE_OS_ENFORCE_LEASE=1` 才启用）。

- `migrations/` 是数据库迁移的唯一事实源：新增迁移先 `reserve-migration` 预留编号，并通过 `tools/check_migrations.py`；`sync`/`publish` 会自动把并行冲突的编号改名到空号。详见 [`migrations/README.md`](migrations/README.md)。
- 任务清单和证据在 `trosa-tasks/<id>.json`、`<id>.verify.log`（共享 git 目录里，不进版本库）。

## 运行与数据边界

- **Agent 读业务数据的现成入口**：先运行 `python3 tools/trosa_cli.py whoami`；通过就用该 CLI（只读 + 提议，提议由成员在 Trosa 确认）。没有令牌时请成员本人按 [`docs/AGENT_ACCESS.md`](docs/AGENT_ACCESS.md) 领取，Agent 不得索要访问码或绕过。
- **维护者全员只读通道**：需要跨成员读取真实数据（排查排除清单、重复客户、迁移核对等）时，用 `python3 tools/trosa_maint_read.py`（独立只读数据库角色，凭据在 `~/.config/trosa-maint/readonly.json`，与 `workbench.env` 分开；开通与边界见 [`docs/MAINTAINER_DATA_ACCESS.md`](docs/MAINTAINER_DATA_ACCESS.md)）。不要为了读数据去读 `workbench.env`。
- **Trosa / Sela 业务数据访问优先顺序**：系统已为 Agent 提供后端接口和内部数据访问能力。处理业务数据必须优先遵循 `现有 API / service → database / internal tools → CLI → UI（仅作最后验收）`。除非任务明确要求 UI 验收，禁止使用浏览器、Computer Use 或模拟人工点击读取或修改业务数据。已有 API / service 能完成操作时，必须调用现有接口，不得绕过接口改用页面操作。
- **正式服务器（腾讯云 Lighthouse，SSH 别名 `trosa-new`）是唯一正式运行环境与唯一写入主机**：Flask/Waitress 位于 `/opt/trade-os/current`，正式业务数据位于服务器本机 PostgreSQL，附件、导入来源和历史回滚材料位于 `/var/lib/trade-os`，通过 Cloudflare Tunnel 对外提供服务。
- **正式运行契约是 `trosa-postgresql-v1`**：唯一正式入口是 `serve.py`，要求 `CRM_ENV=production`、`TRADE_OS_DATA_BACKEND=postgres` 和非空 `TRADE_OS_DATABASE_URL`；`/api/network/ping` 必须报告 `status=ok`、`backend=postgresql`、`formal_runtime=true` 和该契约名。缺配置时在迁移、调度器和端口启动前失败，不得隐式回退 SQLite。
- 本地仅用于开发、隔离测试和已验证发布；不得修改正式服务器数据、正式备份或当前 `data/` 链接所指的运行数据。测试必须使用独立的 `CRM_DB_PATH`。
- 业务数据单一事实源是正式服务器上的 PostgreSQL；SQLite 仅用于隔离开发、导入/导出和明确批准的历史恢复边界。Excel 仅用于导入、导出和历史恢复；Apple 日历 ICS 订阅已于 2026-10-01 退役，不再提供。
- `app.py`、`desktop.py` 和根目录启动器不是正式启动命令：根目录入口只打开已验证的正式工作台；需要 SQLite 旧形状回归时必须显式设置 `CRM_ENV=development TRADE_OS_DEV_SQLITE=1`，PostgreSQL 演练使用 `serve_rehearsal.py` 或 `tools/postgres_rehearsal.py`，并且只允许 loopback 固定演练库。
- 修改前先查看 `git status --short`。工作区可能有用户未提交的改动，绝不覆盖、回退、删除或格式化无关文件。
- 自动发布时只传入已完成的 commit 或 branch，不传文件路径；release 候选在独立临时 worktree 中组装，主工作区已有的无关修改必须保留，不能混入发布。
- 发布配置由 `deploy/cloud/release-env.sh` 统一解析；不要在脚本里各自硬编码 `workbench.env` 路径。开发 Agent 没有发布配置即没有发布能力。

## 核心模块与不可破坏能力

- 固定核心：客户、联系人、沟通时间线、待办/Today、Inbox、Search、日历、归档恢复、导入导出与备份。
- 未配置 AI 时，上述核心必须完整可用；核心页面不能等待 AI、网站读取、邮箱验证或后台任务。
- 写入必须绑定已认证用户；跨用户读取只可返回白名单字段。客户/联系人匹配只允许规范化后的唯一精确邮箱或手机号自动确认，其余只能进入候选/待审阅。
- 保存后优先局部更新。大列表分页或按需加载；客户详情先加载摘要和最近记录，完整时间线、联系人、文件按需加载。
- 所有核心页面要区分加载、空、未登录、权限、网络、服务与解析错误，不能把失败显示成“没有数据”。

## Sela 与 AI

- Sela 负责 prospect 开发：发开信、读邮箱检测回信，检测到回信后交接给 Trosa；退信由 Sela 处理（标记 BOUNCED、停止发信）。它通过受限 `/api/integrations/sela/*` 接口同步事实，使用精确身份匹配、幂等键和 `REVIEW` 处理歧义；不得直接读写 SQLite。Sela 本地只保留 Agent session、delivery journal、outbox、诊断和安全暂停等执行状态，不维护客户/联系人/任务/阶段或人工判断副本；它只接受 Trosa 的 `trosa-postgresql-v1` 健康契约。
- AI/Sela 应预填来源、时间、渠道、证据、摘要和候选，优先减少手工操作；无人值守的后台自动化（Sela、AI 预填、每日跟进建议、定时任务）不得自动创建客户、联系人、待办或商业承诺，每日跟进建议只能落在建议层，不得堆积成逾期（15/30/60 天机械节点的失败不能重演）；成员本人指令下、持有 `crm:write` 令牌的 Agent 可以直接新增、修改、调整日程、归档/恢复，所有写入保留来源、审计与撤销。永久删除仍由人完成。
- 没有足够信息时宁可进入 Inbox/待审阅，也不要自动执行高风险业务动作。
- Trosa 自带的 Gmail 同步（`gmail_sync.py`）当前未配置、已停用；客户回信与退信由 sela 读取邮箱，经 `/api/integrations/sela/reply` 交接。

## 已冻结与不应扩张的内容

- 网站监控、客户 AI 研究/推荐不再是活动用户模块；历史数据保留。
- 15/30/60 天自动开发节点的历史行只保留用于数据/迁移审计；正式运行不再创建、展示或操作。
- 不恢复独立 Pi 前端运行时，也不做独立 AI 聊天窗口；按客户追问 AI 嵌在今天和客户页里。保留的 `/api/agent/*` 只能做原子读取和确认式提案。
- 不重新引入大量客户状态、等级、标签、复杂视图或批量管理作为前台工作负担；历史数据表和迁移历史保留，已退役的死运行接口和入口可以删除。

## 修改原则

- 不因 `app.py` 或 `app/static/app.js` 很大就进行拆文件或大规模重构。先完成一个完整、可验证的问题，复用现有端点和数据模型。
- 不恢复冻结功能，除非用户明确要求。
- 任何功能改动都不得破坏客户、联系人、沟通记录、Today、Inbox、Search、备份恢复或 Sela 的幂等同步契约。
- 涉及 UI 时先读 `design/DESIGN_WORLD.md`（现行基准：瑞士式外观 + 今天潮汐 / Inbox 对话 / 客户账页 / 客户工作区的页面结构，事实来源 `app/static/swiss-d.css`）和 `design/UI_QUALITY_BASELINE.md`；旧 Daylight 规范、房间文档与原型已删除，不再作依据；真实浏览器验收宽屏、iPad、iPhone、旧 Win10、键盘、200% 缩放与性能模式。
- 界面文案遵循「说人话」与术语表，见 [`PRODUCT_DESIGN_STANDARD.md`](PRODUCT_DESIGN_STANDARD.md) §13 交互与语言 和 [`design/UI_QUALITY_BASELINE.md`](design/UI_QUALITY_BASELINE.md) §4 按钮与文案；新页面按此写。
- 写入、导入、附件、备份、恢复必须保留来源、校验与审计；删除必须有明确目标、确认与可恢复快照。

## 开发与验证

首次或依赖变更后：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd browser-extension && npm install
```

修改前至少保存当前状态；修改后按影响范围运行：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m py_compile app.py db.py scheduler.py
node --check app/static/app.js
(cd browser-extension && npm test)
```

后端、前端、数据迁移、性能和发布还须遵循 `TROSA_MAINTENANCE.md` 的最低验证与正式服务器发布检查。用户可感知的功能变动同步记录到 `CHANGELOG.md`；仅开发环境或开发文档变动无需伪造产品变更日志。

界面缺陷必须先在真实浏览器里重现再修：按 [`docs/REAL_BROWSER_REPRO.md`](docs/REAL_BROWSER_REPRO.md) 用隔离服务和 `tools/repro_*.cjs` 脚本重现，修复前脚本须以退出码 20 报告缺陷，修复后同一脚本须退出 0，证据存入 `docs/repro/<场景>/`。仅凭 jsdom 或读代码，不能宣称界面问题已修复。
