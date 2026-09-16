# ECS + Cloud Assistant 发布流程

> 正式发布统一入口：`deploy/cloud/trosa-release`。Codex、Qoder、人工、其他
> Agent 一律使用它；不要手写 SSH 命令、不要直调底层脚本。
>
> ```bash
> deploy/cloud/trosa-release status --json   # production / previous / migration / backup / health
> deploy/cloud/trosa-release publish --commit <sha> [--release-id ID]
> deploy/cloud/trosa-release rollback        # 切回 previous_healthy（数据库不降级）
> ```
>
> 原理（一句话）：本机只做“发射 + 轮询”，真正的发布在 ECS 上由
> `release-remote.sh` 以后台任务幂等执行；传输强制走 Alibaba Cloud Assistant
> 的 `RunCommand` / `DescribeInvocations`，不使用 SSH、Workbench session 或
> 实例 root 密码。22 端口不再是发布链路——本机断网、Agent 退出后重进，都只是重新
> 轮询同一个 release。每次发布对应 `releases/<id>/release.json` 与
> `DEPLOY_RESULT.json`（release/commit/mode/phase/status/production/
> previous/backup/migration/health/error/next_action），`status --json`
> 直接给出机器可读结论。
>
> 数据库分类（`tools/release_db_plan.py`，本地与服务端共用同一定义）：
> `none`（无变化，不备份）、`compatible`（新增前向迁移/数据回填/索引替换，
> 自动做服务端预迁移备份后执行）、`destructive`（迁移时执行的 DROP
> TABLE/COLUMN、TRUNCATE、DELETE 等数据丢失操作，拒绝自动发布，需明确
> `--allow-destructive-db`）、`sensitive_runtime`（迁移代码变化但无新文件，
> 按 compatible 处理）。触发器函数体内的行同步 DELETE 与 DROP
> INDEX/CONSTRAINT 不算破坏性。备份是服务端本地快照（发布链路内），下载
> 到 Mac 的异地归档只在需要时用 `backup-workbench.sh` 按需拉取，不再是
> 每次发布的前置步骤。
>
> `publish-workbench.sh` / `rollback-workbench.sh` 已冻结为兼容垫片（会打印
> DEPRECATED 警告），待新机制经一次真实发布验证后删除。

Trade OS 使用一台持久磁盘 ECS、一个 Waitress 进程、一个 PostgreSQL 写入源和一个 Cloudflare Tunnel。不要启动第二个 Trade OS 实例，也不要把 `data/`、`.env` 或 `.venv` 上传到发布包。ECS 主机防火墙只允许 SSH，应用只监听 `127.0.0.1:8080`；PostgreSQL 只监听 ECS `127.0.0.1:5432`。

首次配置：

```bash
cp deploy/cloud/workbench.env.example deploy/cloud/workbench.env
# 编辑实例 ID 等路由信息，不要写入任何密钥
deploy/cloud/bootstrap-workbench.sh
```

Cloud Assistant 使用 Workbench 的本机受保护 AK profile（`~/.workbench/config.json`，
权限 0600）签名 API；`workbench.env` 永远只放实例路由信息。首次切换可运行
`cloud-assistant-bootstrap.sh` 作为一条 **root Cloud Assistant** 命令：它会创建不可
登录的 `trosa-operator`，日常 status/logs 以该用户执行；发布只允许它 sudo 到一个
固定、参数校验过的包装器，包装器仍只启动已有 `release-remote.sh`。

同一 bootstrap 还安装 `sudo /usr/local/lib/trosa/db-plan-readonly`：它不接受参数，
只切换至 `tradeos` service account，对当前 release 的 migration ledger 执行固定
`SELECT name` 并调用同一份 `release_db_plan.py`。输出仅为脱敏 JSON（ledger 状态、
已应用范围/count、pending、category、destructive files）；不输出 DSN、密码、
PGPASSFILE 内容或其他环境变量，也不能执行 SQL、迁移、重启或切换 release。
安装该只读能力时需把经过审阅、已推送的 Trosa commit 作为
`TROSA_DB_PLAN_COMMIT` 传给 root bootstrap；ECS 会从该精确公开 commit 安装同一份
`release_db_plan.py` 和只读 migration 清单到 root 管理的位置，旧 release 没有 planner
或尚未包含候选迁移时也不会复制一套规则。该安装不切换运行 release。

首次配置还需要由维护者安全写入 `/etc/trade-os/trade-os.env`、`/etc/cloudflared/config.yml` 和 Tunnel 凭据，然后再启用：

```bash
sudo systemctl enable --now trade-os cloudflared
```

当前正式入口为 `https://app.trosa.space`。正式业务数据位于 ECS 的
PostgreSQL（`/opt/trade-os-postgres`），应用通过 systemd drop-in 注入的
DSN 连接；`/var/lib/trade-os` 仅保存客户附件、导入来源和历史回滚材料。Tunnel
凭据只保存在 `/etc/cloudflared/`，不会进入代码仓库。

## 与 sela 的稳定同步契约

云端发布后，sela 通过 Hamid 专用 Bearer 令牌访问以下接口：

- `GET /api/integrations/sela/health`：轻量健康检查和契约版本；
- `GET /api/integrations/sela/exclusions`：带 ETag 的排除索引，不传输整张客户表；
- `GET/POST /api/integrations/sela/prospects`：读取或幂等写入已确认的候选及其来源证据；
- `POST /api/integrations/sela/reply`：记录已确认的真实外联结果；
- `POST /api/integrations/sela/follow-up`：提交需要人工核对的跟进提案。

当前 `sela-v2` 写入接口会在一个 PostgreSQL 事务内完成精确身份匹配、联系人、来源备注和真实外联时间线，并保存 `X-Idempotency-Key` 回执。sela 在网络超时后可以安全重放同一事件，不会重复创建客户或开发信；官网身份按完整规范化域名比较，不使用子串匹配。多重命中、外部身份冲突和邮箱属于另一客户时会返回 `REVIEW`，由人工处理。

发布和验收顺序：

1. 先通过 `deploy/cloud/publish-workbench.sh` 发布包含 `sela-v2` 的 Trosa commit。服务启动时会自动执行增量数据库迁移，既有数据不会被重建。
2. 在 Mac 的 sela 项目中运行 `python3 tools/lab.py trosa-status`，确认健康检查中的 prospect/exclusion 契约版本，再按需运行对应的重试命令处理历史积压。
3. 观察 `data/trosa_auto_sync_state.json` 和 `data/server.log`；只有成功同步的 candidate 才会从 retry queue 消失，`review` 必须人工确认。

如果发布健康检查失败，发布脚本会切回上一份 release；若已经有新契约写入数据，不要把旧代码作为长期运行版本，应重新发布包含 `sela-v2` 的版本。数据库新增字段和回执表是向后兼容的，发布前不需要停掉本地 sela。

公司局域网继续使用 `http://192.168.0.58:8080` 查看只读周报，但该地址现在由公司 Mac 上的 `com.tradeos.weekly-lan` 提供。Mac 只把允许的周报读取请求转到本 ECS，并用独立随机密钥证明来源；ECS 环境只保存 `CRM_WEEKLY_GATEWAY_TOKEN_SHA256` 摘要。Mac 不运行第二个 Trade OS、不读取本地旧数据库，离开公司网络后也不会监听该地址。安装和验收步骤见根目录 `DEPLOYMENT.md`。

代码同步与云端发布现在由 `auto-publish.sh` 串联：Codex 在任务完成并通过本地验证后，显式传入本次改动文件，脚本自动提交到 `main`、推送公开的 `Sisyphux/trosa`，读取发布前 ECS 状态，再通过 Cloud Assistant 发布同一个 commit 并检查公网健康。普通改动不需要人工批准；明确的本地-only 请求和疑似破坏性数据库操作除外。

日常自动入口：

```bash
deploy/cloud/auto-publish.sh --message "说明本次变化" -- FILE1 FILE2
```

脚本不会使用 `git add .`，会拒绝运行数据、密钥、本地环境文件和已跟踪的其他未暂存修改；数据库敏感改动会先运行 `backup-workbench.sh`。`--dry-run` 只执行本地回归、只读 ECS 状态和远程基线检查，不会备份、提交、推送或发布。

底层 `publish-workbench.sh` 仍可用于发布已提交且已推送的本地 `HEAD`；当前仓库公开，因此 ECS 可以直接下载对应 commit 的 GitHub 归档。仓库目前没有 GitHub Actions 或 Webhook 自动部署。

日常操作：

```bash
deploy/cloud/status-workbench.sh
deploy/cloud/logs-workbench.sh
deploy/cloud/auto-publish.sh --message "说明本次变化" -- FILE1 FILE2
deploy/cloud/publish-workbench.sh
deploy/cloud/rollback-workbench.sh
deploy/cloud/backup-workbench.sh
```

`status-workbench.sh` 会先输出 `TROSA_MANAGER_STATUS` 和
`TROSA_MANAGER_RESOURCE` 两行稳定字段，分别供工作台读取服务可用性、`sela` 同步契约版本以及
CPU、内存、根分区磁盘、负载和运行时间；后面的 systemd、磁盘和日志内容仍用于技术排查。
只读状态检查使用 Cloud Assistant API，因此本机 SSH 不可握手、22 端口关闭或 Workbench
实例认证失败时，仍可查询网站和服务器状态。每条命令都有 InvokeId/CommandId，可在网络
恢复后用 `DescribeInvocations` 查询同一远端执行结果；不保留交互式 shell。

发布前建议先查看状态；发布失败时脚本会自动回退，手动回退使用：

```bash
deploy/cloud/status-workbench.sh
deploy/cloud/publish-workbench.sh
deploy/cloud/rollback-workbench.sh
deploy/cloud/logs-workbench.sh
```

`auto-publish.sh` 是日常入口；`trosa-release publish` 是统一发布器。`auto-publish.sh`
先做本地回归、只读 ECS 状态、提交并推送，再调用 `trosa-release publish`
发布同一个 commit。底层发布器让 ECS 后台任务下载指定 commit 的公开归档，
按阶段执行：fetch → db-plan（显式迁移分类）→ backup（仅有数据库变化时，
服务端本地快照）→ migrate（切换流量之前）→ activate（原子切换 `current`
符号链接并重启）→ health（契约 + 页面 + systemd + 迁移账本 +
release 指针五项深度检查）；失败时按阶段自动回滚代码并写入机器可读结果。
ECS 发布锁保证同一时间只有一个 release 在执行；同一 release 重复执行是
幂等的（已是生产版本且健康时直接返回 success）。

`backup-workbench.sh` 会调用 ECS PostgreSQL 生产目录的 verified logical dump，核对 dump
的 SHA-256 和 `pg_restore --list`，再把数据库 dump、客户附件和 manifest 打包下载到 Mac 的
`~/Library/Application Support/trosa/backups/`，核对 bundle SHA-256 后保留最近 14 天的归档。
它不创建阿里云 ECS 系统盘快照；系统盘级灾难恢复需要另外配置云快照或重建 ECS。应用在
PostgreSQL 模式下不会把旧 SQLite 目录伪装成备份，也不会通过备份 API 恢复 SQLite 文件。

## 私有浏览器桌面

服务器还运行一个独立的轻量 XFCE 桌面，供必要时在浏览器中直接操作 Ubuntu。它使用
`trosa-desktop` 无 sudo 权限账户，与 Trosa 的 `tradeos` 数据账户隔离；TigerVNC 与 noVNC
只监听服务器 `127.0.0.1:5901/6080`，没有 ECS 公网端口、UFW 规则或 Cloudflare Tunnel 路由。

从 Mac 通过 `trosa-desktop` SSH 主机别名打开本机隧道，再访问
`http://127.0.0.1:6080/vnc.html`。服务器工作台已将此动作做成“打开服务器桌面”。服务文件、
停用方式和验收要求见 [desktop/README.md](desktop/README.md)。

数据库仍是单写入源。不要在 Mac 上重新启动旧的正式应用并通过同一个域名使用；当前 Mac 的三个旧 LaunchAgent 已禁用但文件保留。如需回退到 Mac，先停止 ECS Tunnel，再执行：

```bash
USER_ID=$(id -u)
for label in com.tradeos.app com.tradeos.tunnel com.tradeos.health; do
  launchctl enable "gui/$USER_ID/$label"
  launchctl bootstrap "gui/$USER_ID" "$HOME/Library/LaunchAgents/$label.plist"
done
```

## 多 Agent 任务隔离

多个 Agent 共用同一个 working tree 时，改动、暂存、测试会互相污染，还会触发
`auto-publish.sh` 的保护门禁导致谁都发布不了。`agent-worktree.sh`
给每个任务独立的 worktree + 独立分支（`agent/<id>`，目录默认在仓库同级的
`trosa-worktrees/`，`workbench.env` 从不复制进隔离区）：

```bash
deploy/cloud/agent-worktree.sh create --task <id>   # 建隔离区，复用主仓 .venv/node_modules
deploy/cloud/agent-worktree.sh test --task <id>     # 隔离数据目录跑完整回归（含扩展测试）
deploy/cloud/agent-worktree.sh sync --task <id>     # 变基到最新 main
deploy/cloud/agent-worktree.sh publish --task <id> --message "说明"  # 合入并发布
deploy/cloud/agent-worktree.sh remove --task <id>   # 回收（默认保留分支）
```

发布没有放宽任何门禁：任务区与主工作区必须都没有已跟踪改动（否则拒绝，
不会卷入他人在途工作）；合入用 `merge --no-commit --no-ff`（冲突则 abort）；
之后全权委托未经修改的 `auto-publish.sh --staged` 走完回归、ECS 状态、备份、
提交、推送、`trosa-release publish` 与公网健康检查。完整说明见
`agent-worktree.sh --help` 与脚本头注释。
