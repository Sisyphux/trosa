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

入口隔离是硬护栏，不只是一条约定：`agent-worktree.sh` 会把版本化的 git 护栏
（`deploy/cloud/git-hooks/`）安装到共享 git 目录，两层一起生效：
`pre-commit` 拒绝 `dev`/`review` 角色在集成分支（默认 `main`）上的提交；
`commit-msg` 兜底拒绝集成分支上任何 `[<id>]` 任务提交（即使会话忘记设置角色）。
必须先 `create`/`adopt` 进入 `agent/<id>` 隔离区；`release` 角色与人工集成提交不受
影响（人工集成可用 `TRADE_OS_ALLOW_MAIN_COMMIT=1`）。`create`/`adopt` 本身也只能在
主工作区执行，不能在某个任务隔离区里再建任务。

### Agent 权限（`TRADE_OS_AGENT_ROLE`）

| 角色 | 允许 | 不允许 |
|---|---|---|
| 开发 Agent `dev` | create/adopt/test/sync/remove、改本任务代码、commit | publish、读发布配置、改主工作区 |
| 审查 Agent `review` | `status` / `preflight` / `test`、查看 `evidence` | 改代码、publish |
| 发布 Agent `release` | 主工作区集成、`publish`、真实验收 | 在主工作区写业务代码 |

- 开发/审查会话开始前设置 `export TRADE_OS_AGENT_ROLE=dev`（或 `review`）；未设置时默认按 `release` 处理，以保持人工操作不变。
- 发布配置 `workbench.env` 的正式位置是 `~/.config/trosa/workbench.env`（仓库外，权限 0600），由 `deploy/cloud/release-env.sh` 统一解析；开发 worktree 不携带它，因此没有发布能力。仓库内旧位置仍兼容读取并提示迁移。
- 角色变量是同机护栏，真正边界是文件权限：发布配置不在仓库里。`publish` 对 dev/review 角色会明确拒绝，而不是静默放行。

## 2. 开新任务（每个 Agent 会话开始时）

```bash
cd ~/Desktop/Trosa
deploy/cloud/agent-worktree.sh status        # 先确认自己在哪个环境（同时刷新入口护栏）
deploy/cloud/agent-worktree.sh preflight     # 体检：主区是否干净、迁移编号是否冲突
deploy/cloud/agent-worktree.sh create --task <id> \
  --owner <负责人/Agent 名> \
  --goal "一句话目标" \
  --scope "预期修改的文件/模块"
```

创建后进入隔离目录工作，并再次确认身份：

```bash
cd ../trosa-worktrees/<id>
deploy/cloud/agent-worktree.sh status   # 确认“任务=<id>”再动手
```

任务清单保存在共享 git 目录 `trosa-tasks/<id>.json`（负责人 / 目标 / 修改范围 / 预留迁移号），
它不进入版本库、不参与发布，因此不会污染 `git status`。

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
deploy/cloud/agent-worktree.sh sync --task <id>    # 消解迁移编号碰撞 + 变基到最新 origin/main
deploy/cloud/agent-worktree.sh test --task <id>    # 与发布候选同一份门禁，并记录证据
```

- commit message 以 `[<id>]` 开头，来源一眼可辨；一次 commit 只承载一个任务的改动。
- **先同步、后门禁**：`sync` 会在变基前自动消解迁移编号碰撞，变基后旧的门禁证据
  立即失效；`test` 只有在 HEAD 已包含最新 `origin/main` 时才通过。反过来先 test
  再 sync 会让证据过期，必须重新 test。
- 测试失败或任务暂停只影响本隔离区，其它任务与主工作区不受影响。
- **完成证据**：`test` 与 `publish` 会把 tree commit、门禁结果和发布 release 写入共享文件 `trosa-tasks/<id>.verify.log`，并更新任务清单状态；用 `evidence --task <id>` 查看。
- **完成定义**：`status=landed`（已发布且健康）才算任务完成；绿色门禁只代表“开发完成”，不能用“已修复”描述尚未发布的改动。`gate --task <id>` 可随时只读检查任务是否 ready（证据对应当前 HEAD 且已包含最新 main）。
- 回收：`remove --task <id>`（默认保留分支；确认丢弃加 `--force`）。

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

## 6. 迁移（数据库结构）并行规则

- **唯一事实源是 `migrations/` 目录**：运行时（`db.py`）与演练工具
  （`tools/unified_postgres_migration.py`）都按文件名排序自动发现，没有第二份清单。
- 新迁移编号在 `create` / `adopt` 时统一预留（跨主工作区与所有隔离区取下一个空号），
  写入任务清单的 `reserved_migration`。预留是原子操作：`agent-worktree.sh` 在共享
  锁内完成“扫描最大编号 + 写任务清单”，因此两个并发任务不会拿到同一个号。
- 每棵树、每次发布前由 `tools/check_migrations.py`（已接入 `release-test.sh` 快速门禁）
  校验：文件名合法、编号唯一。编号空档只作为警告（并行任务可能先发布较大编号，
  空档不会让运行时漏掉任何迁移）。
- 两个任务抢到同一编号时：保留先发布者的编号，后发布者在合并前 `git mv` 到下一个空号，
  不要复制对方的 DDL。跨任务冲突用 `preflight` 可直接看出。
- **自动消解**：`sync` 与 `publish` 会调用 `tools/reconcile_migrations.py`，把本任务新增
  且与最新 main / 其它 worktree / 他人预留冲突的迁移改名到下一个空号并提交；无冲突时
  不改动任何文件。也可单独运行 `agent-worktree.sh reconcile --task <id>`。
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
- **旧的任务分支 / 隔离区堆积？** → `list` 查看，确认已发布后用 `remove --task <id> --delete-branch` 回收。
