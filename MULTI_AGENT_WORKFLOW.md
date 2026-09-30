# Trosa 多 Agent 并行开发流程

> 唯一正式说明。任何 Agent / 开发会话在改动代码前先读这一页。
> 命令实现：`deploy/cloud/agent-worktree.sh`；发布实现：`deploy/cloud/release-commit.sh`。

## 1. 目录边界与 Agent 权限

### 目录边界

| 角色 | 目录 | 允许做什么 | 不允许做什么 |
|---|---|---|---|
| 主工作区 | `~/Desktop/Trosa` | 集成、验收、发布（fetch / merge / cherry-pick / release-commit） | 直接写业务代码、堆叠来源不明的未提交改动 |
| 任务隔离区 | `<仓库同级>/trosa-worktrees/<id>` | 本任务改代码、跑测试、commit | 改其它任务目录、修改主工作区 |
| 发布候选 | 临时 release worktree | 由 `release-commit.sh` 自动创建与回收 | 人工进入修改 |

原则：**一个任务 = 一个 worktree = 一个 `agent/<id>` 分支 = 一个逻辑完整的 commit（或一组 commit）**。
主工作区只承载集成结果，始终保持干净或只有明确归属的发布操作。

入口隔离是硬护栏，不只是一条约定，分任务开始时和提交时两层：
`agent-worktree.sh guard` 是**任务开始闸门** —— dev/review 角色如果在主工作区
（或任何非 `agent/<id>` 目录）运行它，会被明确拒绝并要求先 `create`/`adopt`，
因此在改写任何文件之前就会发现走错目录，而不是等到 commit；`release` 角色与
人工集成调用 `guard` 不受影响，只读的 `status` 也不受影响。
`agent-worktree.sh` 还会把版本化的 git 护栏（`deploy/cloud/git-hooks/`）安装到
共享 git 目录，作为**提交时兜底**：`pre-commit` 拒绝 `dev`/`review` 角色在集成分支
（默认 `main`）上的提交；`commit-msg` 再拒绝集成分支上任何 `[<id>]` 任务提交（即使
会话忘记设置角色）。必须先 `create`/`adopt` 进入 `agent/<id>` 隔离区；`release` 角色
与人工集成提交不受影响（人工集成可用 `TRADE_OS_ALLOW_MAIN_COMMIT=1`）。
`create`/`adopt` 本身也只能在主工作区执行，不能在某个任务隔离区里再建任务。
护栏只在会改动仓库状态的写命令（`create`/`adopt`/`start`/`reserve-migration`/
`reconcile`/`sync`/`test`/`ship`/`publish`/`remove`/`hooks`）里刷新；只读命令
（`status`/`guard`/`preflight`/`list`/`evidence`/`gate`/`flakes`）不写共享 `.git/hooks`，
因此只读调用没有副作用。

### 一个隔离区同一时刻只归一个会话（会话租约）

两个 Agent 会话共写同一个 `agent/<id>` 隔离区，会在“要改的文件正好是对方未提交的文件”
时卡死：`git add` 会把对方 stage 的工作扫进自己的 commit（AGENTS.md 禁止），继续写又会
互相覆盖，最后只能停下来问人。所以租约把它挡在写文件之前：

- `guard` 在任务隔离区里会认领租约（`trosa-tasks/<id>.lease`，按 `TRADE_OS_AGENT_SESSION`
  或 `CLAUDE_CODE_SESSION_ID` 识别会话）；`pre-commit` 提交时再检查一次，并在空闲时自动认领。
- **写文件前也查**：仓库里的 `.claude/settings.json` 给 Edit/Write/NotebookEdit 挂了 PreToolUse 钩子
  （`deploy/cloud/agent-lease-pretool.sh`）。目标文件在别人占用的隔离区里，写入直接被拒绝（退出码 2，
  提示回传给模型）；没跑过 `guard` 的会话在第一次写入时也会自动认领。目标不在 `agent/*` 隔离区、
  输入异常或脚本故障一律放行，护栏自身出问题不会阻断开发。
- 另一个存活会话已持有 → `guard`/`commit` 以退出码 42 拒绝，并直接打印分叉命令。**遇到这个提示不要
  停下来问人，也不要在原目录继续写**，照做即可：

  ```bash
  cd ~/Desktop/Trosa
  TRADE_OS_AGENT_ROLE=dev deploy/cloud/agent-worktree.sh create \
    --task <id>-b --base agent/<id> --owner <name> --goal "..." --scope "..."
  ```

  分叉任务基于原任务**已提交**的 HEAD，看不到对方未提交改动；若要改的文件正是对方未提交的文件，
  等对方 commit 后在分叉任务里 `sync`，不要抢。两个分叉最终各自 `test`/`publish`，冲突走普通 git 合并。
- 持有者进程已退出或心跳过期（有 pid 时 12 小时、无 pid 时 30 分钟）会自动视为空闲，可直接认领。
  只有人工确认对方会话确已停止，才用 `guard --takeover` 接管。
- `lease show` 只读查看，`lease release` 在任务收尾时释放；`remove` 会一并清理。
- 没有会话标识（人工终端）时租约不启用；确需共用同一目录可设 `TRADE_OS_ALLOW_SHARED_WORKTREE=1`。
- 每个 Agent 会话应对应**自己的**任务 id：接手别人的任务先看 `status` 里的“会话租约”行，
  不要因为目录已存在就直接进去写。

### Agent 权限（`TRADE_OS_AGENT_ROLE`）

| 角色 | 允许 | 不允许 |
|---|---|---|
| 开发 Agent `dev` | create/adopt/test/sync/remove、改本任务代码、commit | publish、读发布配置、改主工作区 |
| 审查 Agent `review` | `status` / `preflight` / `test`、查看 `evidence` / `flakes` | 改代码、publish |
| 发布 Agent `release` | 主工作区集成、`publish`、真实验收 | 在主工作区写业务代码 |

- 开发/审查会话开始前设置 `export TRADE_OS_AGENT_ROLE=dev`（或 `review`）；未设置时默认按 `release` 处理，以保持人工操作不变。
- 发布配置 `workbench.env` 的正式位置是 `~/.config/trosa/workbench.env`（仓库外，权限 0600），由 `deploy/cloud/release-env.sh` 统一解析；开发 worktree 不携带它，因此没有发布能力。仓库内旧位置仍兼容读取并提示迁移。
- 角色变量是同机护栏，真正边界是文件权限：发布配置不在仓库里。`publish` 对 dev/review 角色会明确拒绝，而不是静默放行。

## 2. 开新任务（每个 Agent 会话开始时）

推荐用一个命令走完“闸门 + 环境 + 体检 + 建区”：

```bash
cd ~/Desktop/Trosa
deploy/cloud/agent-worktree.sh start --task <id> \
  --owner <负责人/Agent 名> \
  --goal "一句话目标" \
  --scope "预期修改的文件/模块"
```

`start` 要求主工作区干净；有在途改动时会拒绝并提示改用 `adopt`（它专门搬运在途
改动并保留 stash 备份），避免把来源不明的改动留在主工作区。需要分步执行时，下面
四个命令仍然可用（`start` 就是它们的组合）：

```bash
deploy/cloud/agent-worktree.sh guard         # 任务开始闸门：dev/review 在主工作区会被拒绝
deploy/cloud/agent-worktree.sh status        # 先确认自己在哪个环境（同时刷新入口护栏）
deploy/cloud/agent-worktree.sh preflight     # 体检：主区是否干净、迁移编号是否冲突
deploy/cloud/agent-worktree.sh create --task <id> \
  --owner <负责人/Agent 名> \
  --goal "一句话目标" \
  --scope "预期修改的文件/模块"
```

开发/审查会话在动任何文件之前先运行 `guard`：它会告诉你“现在能否开始任务”。
若输出“可以开始任务”，说明你已经在自己的 `agent/<id>` 隔离区；若被拒绝，按提示
先在主工作区 `start`/`create`/`adopt`。`release` 角色或未设置角色（人工集成）运行
`guard` 始终放行。进入隔离区后再确认身份：

```bash
cd ../trosa-worktrees/<id>
deploy/cloud/agent-worktree.sh status   # 确认“任务=<id>”再动手
```

任务清单保存在共享 git 目录 `trosa-tasks/<id>.json`（负责人 / 目标 / 修改范围 / 预留迁移号），
它不进入版本库、不参与发布，因此不会污染 `git status`。

迁移编号是**懒预留**的：`start`/`create` 默认不取号，只有确实要新增 `migrations/`
文件时才运行 `deploy/cloud/agent-worktree.sh reserve-migration --task <id>`（幂等，
已预留则原样返回），避免无数据库改动的任务消耗单调计数器。需要建区即取号可给
`create` 加 `--reserve-migration`。

## 3. 主工作区出现无法归属的在途改动时

不要继续在主工作区开发，也不要随手 commit。用 `adopt` 整体搬进隔离区：

```bash
# 默认搬运全部未提交改动；--path 可只搬指定路径（相对主工作区根）
deploy/cloud/agent-worktree.sh adopt --task <id> --owner <name> \
  --goal "..." --scope "..."
```

`adopt` 会把改动保存为 stash 备份（保留、不删除）、应用到新的隔离区，主工作区恢复干净。
在隔离区确认无误后再 `git stash drop`。任何一步失败都不会丢失原始改动。

## 4. 提交、验证、同步

```bash
# 在隔离区里
git add <只加本任务的文件>
git commit -m "[<id>] 说明这次完成了什么"
deploy/cloud/agent-worktree.sh ship --task <id>    # 交付：同步 + 快速门禁 + 登记发布队列
deploy/cloud/agent-worktree.sh sync --task <id>    # 消解迁移编号碰撞 + 变基到最新 origin/main
deploy/cloud/agent-worktree.sh test --task <id>    # 与发布候选同一份门禁，并记录证据
```

- **交付用 `ship`**：它把 `sync`（含迁移编号消解）→ 快速门禁（`release-test.sh
  --quick`）→ 登记到仓库外发布队列（`trosa-tasks/.ship-queue`，任务状态置为
  `shipped`）串成一个动作，然后立即返回。开发方**不等待**完整门禁，也不自己发布；
  发布方会在自己一侧重跑完整门禁再落地（开发方写下的 `verify.log` 不是判定依据）。
  需要提前看完整门禁结果时仍可单独 `test --task <id>`。

- commit message 以 `[<id>]` 开头，来源一眼可辨；一次 commit 只承载一个任务的改动。
- **先同步、后门禁**：`sync` 会在变基前自动消解迁移编号碰撞，变基后旧的门禁证据
  立即失效；`test` 只有在 HEAD 已包含最新 `origin/main` 时才通过。反过来先 test
  再 sync 会让证据过期，必须重新 test。
- 测试失败或任务暂停只影响本隔离区，其它任务与主工作区不受影响。
- **完成证据**：`test` 与 `publish` 会把 tree commit、门禁结果和发布 release 写入共享文件 `trosa-tasks/<id>.verify.log`，并更新任务清单状态；用 `evidence --task <id>` 查看。Chromium 验收的瞬时失败会记入 `trosa-tasks/.flake-events.log`，用 `flakes [--limit <n>]` 汇总。
- **完成定义**：`status=landed`（已发布且健康）才算任务完成；绿色门禁只代表“开发完成”，不能用“已修复”描述尚未发布的改动。`gate --task <id>` 可随时只读检查任务是否 ready（证据对应当前 HEAD 且已包含最新 main）。
- 回收：`remove --task <id>` 有多道护栏。删除前先检查进程占用：有进程 cwd 或打开
  文件落在该 worktree 内时默认拒绝（打印 pid / 命令行 / 端口 / 目录，绝不自动杀
  进程；缺 `lsof` 时 fail closed），只有显式 `--ignore-processes` 才越过（`--force`
  不隐含它）。随后列出将被删除的 gitignore 内容（按顶层汇总大小，如 `.local`、
  `data`、`.venv`、`node_modules`）与任务清单 `<id>.json`；交互环境要求确认，非
  交互环境必须显式 `--yes`（`--force` 也表示已确认）。默认要求 worktree 干净，
  `--force` 才允许丢弃未提交改动。删除任务清单/证据前会自动备份到
  `trosa-tasks/removed/<id>.<UTC时间戳>.{json,verify.log}`，之后 `evidence --task <id>`
  仍能显示“已回收”与发布结论。默认保留分支；`--delete-branch` 仅对该任务已发布
  （`status=landed`）或在 `<main>` 上能找到等价补丁（`git cherry` 无独有提交）的
  分支安全删除，未等价合入的分支会拒绝并列出独有提交，仅 `--force` 才显式丢弃。

## 5. 合并与发布

发布只接受 commit / 分支，从不读取调用者的工作区：

```bash
# 方式一：发布任务分支（推荐；cherry-pick 到 origin/main 后跑完整门禁）
deploy/cloud/agent-worktree.sh publish --task <id>

# 方式二：显式列出 commit
deploy/cloud/auto-publish.sh --commit <sha> [--commit <sha> ...]

# 只构建候选 + 本地门禁，不推送、不发布
deploy/cloud/auto-publish.sh --dry-run --commit <sha>
```

- 发布候选在临时 release worktree 中 `cherry-pick -x`，release commit 内保留原始
  commit SHA，可反查来源。
- **发布机制必须来自最新 main**：同一个 production commit 不应因为“从哪个 worktree
  发起”而走不同版本的发布机制。`publish` / `auto-publish` / `release-commit` 在真正
  发布前检查自己的发布基础设施（`deploy/cloud/` 与 `tools/release_baseline.py`）是否
  与 `origin/main` 一致；调用者 checkout 落后或有差异时，自动改从 `origin/main` 的干净
  临时 worktree 重新执行正式发布逻辑，读不到 `origin/main` 则明确拒绝（fail closed）。
  任务代码可以领先 main（发布输入本来就是 task commit），但发布机制本身不能。
- **发布只接受真正 ready 的任务**：`publish` 会先校验任务区干净、门禁证据对应当前
  HEAD、HEAD 已包含最新 `origin/main`（并在需要时先消解迁移编号碰撞），任一不满足
  即拒绝并要求 `sync` + `test`。直接调用 `release-commit.sh --branch agent/<id>`
  也走同一判定（`--dry-run` 除外），不能绕过。
- 两个任务同时改同一文件：各自在隔离区提交；先发布者先进入 `origin/main`，后发布者
  `sync` + 解决冲突后再发布。冲突发生在发布候选里，不会污染任何人的工作区。
- 本地 `main` 是否移动不影响发布（发布基线始终是 `origin/main`）。发布后按需
  `git fetch origin && git merge --ff-only origin/main`。
- **并发发布是安全的**：本地用共享 git 目录中的可移植锁（崩溃后可回收）把发布候选
  串行化，推送 `origin/main` 仍用 `--force-with-lease` 固定基线，基线上移时后到者
  被拒绝。ECS 侧发布 runner 也串行执行（有界等待，超时写明确 `busy` 结果）。
- **production 基线门（关键）**：ECS 在切换流量前会证明候选 commit 仍包含当前
  production commit。若候选基于旧 production（例如你在等待期间另一个任务已上线），
  发布会被明确 `refused`，production 不变；你必须 `sync` 到最新 `main`、解决冲突后
  重新 `publish`。这个规则保证后上线者包含先前所有已上线改动，任何一方都不会被静默
  覆盖；无法证明安全的比较一律 fail closed。

### 常驻发布流水线（能力分权，阶段4）

`ship` 只把交付写进队列。消费队列的是一个常驻的、持有发布凭据的发布方
（`deploy/cloud/release-pipeline.sh`），它**自己重新计算一切**，不信任开发方交来的
`verify.log`：

```bash
# 只读：按任务已交付的 commit 相对 origin/main 的改动计算风险级别
deploy/cloud/release-pipeline.sh classify --task <id>
# 只读：打印交付队列与已处置结果
deploy/cloud/release-pipeline.sh status
# 消费队列（默认 dry-run：cherry-pick + 全量门禁 + 数据库预检，不推送不发布）
deploy/cloud/release-pipeline.sh run
# 真正发布（阶段4 只对 T1 生效；要求 release 能力）
deploy/cloud/release-pipeline.sh run --publish
```

- **分级由流水线算，不由开发方声明**（`tools/release_tier.py`，规则属于流水线一侧）：
  - **T0**：只改 `docs/`、`design/`、`tests/` 或以 `.md` 结尾 → 合入 main，不部署；
  - **T1**：其余常规代码 → 门禁绿后自动发布，健康检查失败由 ECS 自动回滚；
  - **T2**：触及 `migrations/`、`deploy/`、`serve.py`、`config.py`、`AGENTS.md`，
    或发布/迁移/备份工具与流水线自身 → 停在 `awaiting-approval`，人工放行后才发布。
    T2 优先于 T0/T1，所以任何流水线自身的改动永远是 T2。
- **不信任开发方的结论**：流水线调 `release-commit.sh --pipeline`，它不要求任务清单里
  已有 `verify_result=ok`，但**强制重跑完整门禁**并禁用“按对象身份复用”，保证门禁是
  流水线本次独立算出的。伪造一份 `verify.log` 或账本记录都无法让流水线跳过门禁。
- **队列与游标**：队列 `trosa-tasks/.ship-queue`（`ship` 追加），游标
  `trosa-tasks/.pipeline-state`（每个 commit 的处置：`landed` / `dry-run-ok` /
  `awaiting-approval` / `merge-only` / `failed` / `refused`）。队列记录必须与任务清单
  的 `shipped_commit` 一致、且 commit 确在该 agent 分支上，否则按伪造处理并拒绝。
- **凭据边界**：流水线自身不存放任何凭据；发布能力来自仓库外的发布配置与云 AK，
  开发会话读不到（阶段4 先在仓库内实现 runner；常驻的 launchd/专用账号与凭据落位
  见后续阶段，需要人工确认）。



一次完整发布门禁仍只有一份实现 `deploy/cloud/release-test.sh`，dev 任务区、release
候选与正式发布共用它。为了不把同一棵树重算一遍，门禁现在按下面的结构执行，并在
“候选与已验收对象逐字节相同”时复用结论；任何一项不匹配立即回到全量，绝不削弱保障。

**执行结构（一次完整门禁）**

- 快速检查（Python/JS 语法、`tools/check_migrations.py` 迁移完整性）先跑，失败即停。
- 之后三条互不依赖的分支并行，输出各自落盘后按分支顺序打印，不交错：
  - 分支 A：隔离 SQLite 的 Python 回归（失败重跑一次，两次都失败才红）；
  - 分支 B：真实 PostgreSQL rehearsal → 真实 Chromium 页面验收 → Inbox 专项验收；
  - 分支 C：浏览器扩展回归（`browser-extension` 的 `npm test`）。
- 只有 Chromium 步骤允许失败重跑一次（`release_gate_run_browser_step`），且事件必须
  落进共享 flake 台账 `trosa-tasks/.flake-events.log`（每行 `<时间>\t<步骤>\t<树>\t
  <首次退出码>\t<结果>`，结果 ∈ `retrying|ok|failed`）：重跑通过门禁继续但事件已记账，
  重跑仍失败整道门禁红——绝不用重跑把失败静默算通过。PostgreSQL rehearsal 与 Python
  回归不适用该机制（前者确定性、后者本就重跑一次）。用 `agent-worktree.sh flakes`
  汇总台账；同一步骤反复出现时按真实缺陷排查，不要依赖重跑。
- 任一支失败都让整道门禁失败：运行器会等待其余分支结束再返回非 0，`trap` 统一回收
  临时数据目录、日志目录与 PostgreSQL rehearsal 服务，不留后台进程。
- PostgreSQL rehearsal 在一条门禁里只起停一次：`release-test.sh` 选定唯一端口并
  拥有服务生命周期；`tools/browser_acceptance.sh` 被门禁调用时以
  `TROSA_BROWSER_ACCEPTANCE_REUSE_REHEARSAL=1` 复用同一服务/连接，只重载确定性
  fixture（集成测试会改动 rehearsal 数据），不再停-起服务。该脚本仍可独立运行
  （不设该变量时行为不变）。

**复用成立的三项锚定（外加基线）**

`test --task` 通过完整门禁后，只有在工作树完全干净（含未跟踪的非忽略文件）时，才把
验收对象登记进共享账本 `trosa-tasks/.verified-trees`（在共享 git 目录里，只登记
`result=ok` 且 key 完整的记录）。记录的身份由四项组成：

- `tree`：候选树的 git tree hash（`HEAD^{tree}`，逐字节内容身份）；
- `gate_impl`：实际执行门禁的实现哈希——运行门禁那份 `deploy/cloud/` 加上候选树
  `tools/`（`browser_acceptance`、`postgres_rehearsal` 等）的路径 + blob 哈希；
- `external`：候选树 `migrations/` 目录（文件名 + 内容）与门禁固定测试环境占位；
- `base`：`origin/main` 的 commit sha。

`release-commit.sh` 在 cherry-pick 之后计算候选身份，命中账本且四项全部一致时跳过
全量门禁，但仍执行一次快速语法/迁移完整性检查，并显式打印
`gate reused for tree=<hash>`；未命中走原全量门禁。判定失败（解析不到身份、账本缺失、
读不到 `origin/main`）一律 fail closed，回到全量。

**什么时候必然回到全量**

- 候选树内容变了（任何被跟踪文件不同，包括任务新增的迁移）；
- 门禁实现变了（`deploy/cloud/` 或候选树 `tools/` 任一文件不同，例如本任务自己
  改发布基础设施时就必然回到全量）；
- 外部输入变了（`migrations/` 目录不同）；
- 基线前进了（`origin/main` 不再是验收时的 commit），即使树巧合相同；
- 任何一项无法计算或账本读不到。

**为什么仍然安全**

- 复用按对象身份，不按任务目录：release 侧只比较 tree/实现/外部输入/基线哈希，
  不读取、也不信任任何任务工作区路径；候选树来自 cherry-pick 后的不可变 commit。
- 复用不绕过健康检查与发布安全：快速语法/迁移完整性检查照跑，production 基线门、
  切换前已校验备份、ECS 深度健康检查、`--force-with-lease` 推送全部不变。
- 被验收的内容必须等于 HEAD 的 tree：`test --task` 遇到脏工作树只标记
  `reusable_tree=0`（`verify_result` 仍可为 ok），不产生可复用证据；发布本身仍要求
  任务区完全干净。
- 批量发布同样安全：`--commit` 输入与 `--branch` 使用同一完成证据判定
  （`verify_result=ok`、证据对应该 commit、且已包含最新 `origin/main`），找不到
  对应证据即拒绝；多个 ready 任务一次 cherry-pick 成同一候选树，门禁只付一次。

## 6. 迁移（数据库结构）并行规则

- **唯一事实源是 `migrations/` 目录**：运行时（`db.py`）与演练工具
  （`tools/unified_postgres_migration.py`）都按文件名排序自动发现，没有第二份清单。
- 新迁移编号是**懒预留**：`start`/`create` 默认不取号，真要写迁移时用
  `reserve-migration --task <id>`（幂等），`adopt` 因搬运的在途改动可能已含迁移而
  仍会取号。取到的号写入任务清单的 `reserved_migration`。分配是并发安全的：
  `agent-worktree.sh` 与 `tools/reconcile_migrations.py` 共用同一个共享预留锁
  （`trosa-tasks/.reserve.lock`），并在锁内写一份持久计数
  `trosa-tasks/.migration-counter`；编号单调递增、从不回收，
  因此两个并发任务（无论是预留还是同时 `reconcile`）不会拿到同一个号，也不
  再只依赖“扫描当前哪个号为空”。
- 每棵树、每次发布前由 `tools/check_migrations.py`（已接入 `release-test.sh` 快速门禁）
  校验：文件名合法、编号唯一。编号空档只作为警告（并行任务可能先发布较大编号，
  空档不会让运行时漏掉任何迁移）。
- 两个任务抢到同一编号时：保留先发布者的编号，后发布者在合并前 `git mv` 到下一个空号，
  不要复制对方的 DDL。跨任务冲突用 `preflight` 可直接看出。
- **自动消解**：`sync` 与 `publish` 会调用 `tools/reconcile_migrations.py`，把本任务新增
  且与最新 main / 其它 worktree / 他人预留冲突的迁移改名到下一个空号并提交；无冲突时
  不改动任何文件。也可单独运行 `agent-worktree.sh reconcile --task <id>`。
- **改号边界（fail closed）**：reconcile 只处理“当前任务相对 merge-base 新增”的文件。
  已经进入 main（目标引用里存在同名文件）的迁移不动；记录在 applied ledger
  （`trosa-tasks/.applied-migrations`，或 `--applied-ledger` 指定的文件）里的迁移视为
  可能已在环境执行，一旦命中即拒绝改号并报错；读不到目标引用的 `migrations/` 目录也
  直接失败。无法安全自动处理时不会猜测，而是给出明确原因让人工新增前向迁移。
- 迁移 **forward-only**：已应用的迁移文件不可再改内容（运行时会因 SHA-256 变化拒绝启动），
  修改必须新增前向迁移。

## 7. 可追溯性

- 每个 release 有唯一 `release-id` 与 commit SHA（`deploy/cloud/trosa-release status --json`）。
- release commit 内含 `(cherry picked from commit <原始 sha>)`。
- 任务清单记录 owner / goal / scope / status；commit 以 `[<id>]` 开头。
- 完成证据 `trosa-tasks/<id>.verify.log` 记录被验证的 commit 与结果；`status` 为 `active` / `landed`。
- 主工作区不干净即流程违规，`preflight` 会报告。

## 8. 常见问题

- **“我是不是走错目录了？”** → 任意工作树里运行 `status`。
- **主工作区又有脏文件？** → `preflight` 列出，`adopt` 搬走。
- **发布冲突了？** → `release-commit.sh` 会 abort 且不动 `origin/main`；在隔离区 `sync` 后重试。
- **任务失败 / 暂停** → 离开隔离区即可，其它任务不受影响；恢复时回到原目录继续。
- **`publish` 报“当前角色没有发布权限”？** → 你的会话是 dev/review 角色；把 commit 和证据交给 release 角色发布，这是设计边界，不是错误。
- **门禁绿了但没人说已修复？** → 只有 `status=landed` 才算完成；`evidence --task <id>` 可核对 commit 与 release。
- **旧的任务分支 / 隔离区堆积？** → `list` 查看，确认已发布后用
  `remove --task <id> --delete-branch` 回收。`--delete-branch` 对已发布任务
  （`status=landed`）或在 `main` 上能找到等价补丁的分支（`git cherry` 无独有提交）
  会安全删除并打印依据；对仍有独有提交的分支会拒绝并列出这些提交，不会因为加了
  `--delete-branch` 就悄悄 `-D`（确认丢弃才加 `--force`）。`publish` 是
  cherry-pick，已发布任务的分支通常不是 `main` 的祖先，所以旧的 `branch -d` 对
  它们必失败——这正是分支堆积的原因，现由等价检查解决。
