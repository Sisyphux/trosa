# Trosa 多 Agent 并行开发流程

> 唯一正式说明。任何 Agent / 开发会话在改动代码前先读这一页。
> 命令实现：`deploy/cloud/agent-worktree.sh`；发布实现：`deploy/cloud/release-commit.sh`。

## 1. 三个角色，三个边界

| 角色 | 目录 | 允许做什么 | 不允许做什么 |
|---|---|---|---|
| 主工作区 | `~/Desktop/Trosa` | 集成、验收、发布（fetch / merge / cherry-pick / release-commit） | 直接写业务代码、堆叠来源不明的未提交改动 |
| 任务隔离区 | `<仓库同级>/trosa-worktrees/<id>` | 本任务改代码、跑测试、commit | 改其它任务目录、修改主工作区 |
| 发布候选 | 临时 release worktree | 由 `release-commit.sh` 自动创建与回收 | 人工进入修改 |

原则：**一个任务 = 一个 worktree = 一个 `agent/<id>` 分支 = 一个逻辑完整的 commit（或一组 commit）**。
主工作区只承载集成结果，始终保持干净或只有明确归属的发布操作。

## 2. 开新任务（每个 Agent 会话开始时）

```bash
cd ~/Desktop/Trosa
deploy/cloud/agent-worktree.sh status        # 先确认自己在哪个环境
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
deploy/cloud/agent-worktree.sh test --task <id>    # 与发布候选同一份门禁
deploy/cloud/agent-worktree.sh sync --task <id>    # 变基到最新 main
```

- commit message 以 `[<id>]` 开头，来源一眼可辨；一次 commit 只承载一个任务的改动。
- 测试失败或任务暂停只影响本隔离区，其它任务与主工作区不受影响。
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
- 两个任务同时改同一文件：各自在隔离区提交；先发布者先进入 `origin/main`，后发布者
  `sync` + 解决冲突后再发布。冲突发生在发布候选里，不会污染任何人的工作区。
- 本地 `main` 是否移动不影响发布（发布基线始终是 `origin/main`）。发布后按需
  `git fetch origin && git merge --ff-only origin/main`。

## 6. 迁移（数据库结构）并行规则

- **唯一事实源是 `migrations/` 目录**：运行时（`db.py`）与演练工具
  （`tools/unified_postgres_migration.py`）都按文件名排序自动发现，没有第二份清单。
- 新迁移编号在 `create` / `adopt` 时统一预留（跨主工作区与所有隔离区取下一个空号），
  写入任务清单的 `reserved_migration`。
- 每棵树、每次发布前由 `tools/check_migrations.py`（已接入 `release-test.sh` 快速门禁）
  校验：文件名合法、编号唯一。编号空档只作为警告（并行任务可能先发布较大编号，
  空档不会让运行时漏掉任何迁移）。
- 两个任务抢到同一编号时：保留先发布者的编号，后发布者在合并前 `git mv` 到下一个空号，
  不要复制对方的 DDL。跨任务冲突用 `preflight` 可直接看出。
- 迁移 **forward-only**：已应用的迁移文件不可再改内容（运行时会因 SHA-256 变化拒绝启动），
  修改必须新增前向迁移。

## 7. 可追溯性

- 每个 release 有唯一 `release-id` 与 commit SHA（`deploy/cloud/trosa-release status --json`）。
- release commit 内含 `(cherry picked from commit <原始 sha>)`。
- 任务清单记录 owner / goal / scope；commit 以 `[<id>]` 开头。
- 主工作区不干净即流程违规，`preflight` 会报告。

## 8. 常见问题

- **“我是不是走错目录了？”** → 任意工作树里运行 `status`。
- **主工作区又有脏文件？** → `preflight` 列出，`adopt` 搬走。
- **发布冲突了？** → `release-commit.sh` 会 abort 且不动 `origin/main`；在隔离区 `sync` 后重试。
- **任务失败 / 暂停** → 离开隔离区即可，其它任务不受影响；恢复时回到原目录继续。
- **旧的任务分支 / 隔离区堆积？** → `list` 查看，确认已发布后用 `remove --task <id> --delete-branch` 回收。
