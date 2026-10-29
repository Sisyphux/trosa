# 服务器发布流程（SSH）

> 正式发布统一入口：`deploy/cloud/trosa-release`。Codex、Qoder、人工、其他
> Agent 一律使用它；不要手写 SSH 命令、不要直调底层脚本。
>
> ```bash
> deploy/cloud/trosa-release status --json   # production / previous / migration / backup / health
> deploy/cloud/trosa-release publish --commit <sha> [--release-id ID]
> deploy/cloud/trosa-release check --release-id ID [--json]  # 按 release 恢复查询
> deploy/cloud/trosa-release rollback        # 切回 previous_healthy（数据库不降级）
> ```
>
> 原理（一句话）：本机只做“发射 + 轮询”，真正的发布在服务器上由
> `release-remote.sh` 以后台任务幂等执行。传输只走 SSH：`deploy/cloud/run-ssh-command.sh`
> 连接 `~/.ssh/config` 中的别名（默认 `trosa-new`，即腾讯云 Lighthouse 43.155.211.57，
> 用户 ubuntu，sudo 免密），不使用阿里云 API、Cloud Assistant、Workbench 或 root 密码。
> 发射时 `release-launch-remote.sh` 随同一条 SSH 命令送到服务器，以 root 执行：校验参数 →
> 从 GitHub 拉取该 commit 的 `release-remote.sh` → `setsid nohup` 后台启动后立即返回。
> 服务器上不需要预装任何发布脚本。22 端口断开、本机断网、Agent 退出后重进，都只是重新
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
> INDEX/CONSTRAINT 不算破坏性。对数据库敏感改动，发布门禁要求切换流量前在
> 服务器生成并校验一份权威备份（`deploy/cloud/backup-remote.sh`：大小 +
> SHA-256 + `pg_restore --list` 可恢复性，可选离机镜像），失败即中止；
> 下载到 Mac 的异地归档是可选项，`scp` 下载失败**不会**阻塞发布。失败分类：
> `backup_failed`（备份失败）、`backup_verification_failed`（备份校验/存储失败）、
> `release_failed`（代码发布失败）与客户端 `local_download_failed`（单纯下载失败）。

Trade OS 使用一台持久磁盘服务器、一个 Waitress 进程、一个 PostgreSQL 写入源和一个 Cloudflare Tunnel。当前正式服务器是腾讯云 Lighthouse（SSH 别名 `trosa-new`）。不要启动第二个 Trade OS 实例，也不要把 `data/`、`.env` 或 `.venv` 上传到发布包。服务器防火墙只允许 SSH，应用只监听 `127.0.0.1:8080`；PostgreSQL 只监听服务器 `127.0.0.1:5432`。

首次配置（本机）：

```bash
mkdir -p ~/.config/trosa
cp deploy/cloud/workbench.env.example ~/.config/trosa/workbench.env
chmod 600 ~/.config/trosa/workbench.env
# 确认其中 TRADE_OS_SSH_HOST=trosa-new（不写任何密钥）
ssh -o BatchMode=yes trosa-new true   # 连通性检查，无输出即正常
```

发布配置放在仓库外（`~/.config/trosa/workbench.env`，或由 `TRADE_OS_WORKBENCH_ENV`
指向），这样任务 worktree、release 归档和误提交都不会携带发布开关。仓库内旧位置
`deploy/cloud/workbench.env` 仍可读取，但会提示迁移；所有脚本通过
`deploy/cloud/release-env.sh` 统一解析，不再各自硬编码路径。发布（`publish` /
`rollback`）还要求 `TRADE_OS_AGENT_ROLE=release`：开发/审查 Agent 会话应设置
`TRADE_OS_AGENT_ROLE=dev` 或 `review`，它们在 `publish` 时会被明确拒绝。

### SSH 与权限

- 认证只用操作者本机的私钥（由 `~/.ssh/config` 的 `IdentityFile` 指定）。脚本不读取、
  不保存任何密钥；`workbench.env` 只放 SSH 别名。
- 服务器端的发布、回滚、备份以 root 经 `sudo -n` 运行（ubuntu 账号的免密 sudo）。
- 状态检查与日志（`status-workbench.sh` / `logs-workbench.sh`）以普通账号只读运行，
  因此迁移账本一栏为 `null`（数据库凭据只有 root 可读）。需要账本信息时再单独授权。
- 传输层退出码契约（`run-ssh-command.sh`）：远端命令的真实退出码；`2` = SSH 会话未建立，
  命令未送达，可直接重跑；`124` = 会话建立后结果未知，**不是**发布失败，先用
  `check --release-id` 恢复查询。

同一个 `workbench.env` 的 `TRADE_OS_SSH_HOST` 决定所有发布、状态、备份、回滚打到哪台
服务器。**迁移注意**：旧阿里云 ECS（别名 `trosa-ecs`，47.239.60.195）只作为回退保留，
已停止并禁用；如果本机配置仍写着 `trosa-ecs`，所有脚本都会去那台旧机器，必须改成 `trosa-new`。

首次配置服务器（一次性，由维护者在服务器上完成，不经过发布脚本）：写入
`/etc/trade-os/trade-os.env`、`/etc/cloudflared/config.yml` 和 Tunnel 凭据，然后启用：

```bash
sudo systemctl enable --now trade-os cloudflared
```

当前正式入口为 `https://app.trosa.space`。正式业务数据位于服务器上的 PostgreSQL（`/opt/trade-os-postgres`），应用通过 systemd drop-in 注入的 DSN 连接；`/var/lib/trade-os` 仅保存客户附件、导入来源和历史回滚材料。Tunnel 凭据只保存在 `/etc/cloudflared/`，不会进入代码仓库。

## 与 sela 的稳定同步契约

云端发布后，sela 通过 Hamid 专用 Bearer 令牌访问以下接口：

- `GET /api/integrations/sela/health`：轻量健康检查和契约版本；
- `GET /api/integrations/sela/exclusions`：带 ETag 的排除索引，不传输整张客户表；
- `GET/POST /api/integrations/sela/prospects`：读取或幂等写入已确认的候选及其来源证据；读取由业务事实派生的 `lifecycle_stage` 与 `customer_linked`，不把 `trosa_id`/`customer_type` 当身份；
- `POST /api/integrations/sela/reply`：记录 sela 检测到的客户回信/退信事实；
- `POST /api/integrations/sela/prospects/<source_id>/contact-block`：sela 把仍处于 `cold_prospect` 的 Prospect 设为停止联系（竞品 / 非目标买家）。只能加锁、不能解锁：body 为 `{"reason": "<必填，至少 2 字，写明原因与来源>", "idempotency_key": "..."}`（`X-Idempotency-Key` 同值）；已有回复、已转人工或成交的 Prospect 返回 409，已停止联系的幂等返回当前视图；解禁仍只能由人在 Trosa 操作；

当前 `sela-v2` 写入接口会在一个 PostgreSQL 事务内完成精确身份匹配、联系人、来源备注和真实外联时间线，并保存 `X-Idempotency-Key` 回执。sela 在网络超时后可以安全重放同一事件，不会重复创建客户或开发信；官网身份按完整规范化域名比较，不使用子串匹配。多重命中、外部身份冲突和邮箱属于另一客户时会返回 `REVIEW`，由人工处理。

发布和验收顺序：

1. 先通过 `deploy/cloud/trosa-release publish --commit <sha>` 发布包含 `sela-v2` 的 Trosa commit。服务启动时会自动执行增量数据库迁移，既有数据不会被重建。
2. 在 Mac 的 sela 项目中运行 `python3 tools/lab.py trosa-status`，确认健康检查中的 prospect/exclusion 契约版本，再按需运行对应的重试命令处理历史积压。
3. 在 sela 侧观察 outbox/quarantine 并运行 `python3 tools/lab.py trosa-status`；临时网络失败会在 outbox 重试，被 Trosa 拒绝或过期的事件进入 quarantine，不会自动重放。

如果发布健康检查失败，发布脚本会切回上一份 release；若已经有新契约写入数据，不要把旧代码作为长期运行版本，应重新发布包含 `sela-v2` 的版本。数据库新增字段和回执表是向后兼容的，发布前不需要停掉本地 sela。

公司局域网继续使用 `http://192.168.0.58:8080` 查看只读周报，但该地址现在由公司 Mac 上的 `com.tradeos.weekly-lan` 提供。Mac 只把允许的周报读取请求转到正式服务器，并用独立随机密钥证明来源；服务器环境只保存 `CRM_WEEKLY_GATEWAY_TOKEN_SHA256` 摘要。Mac 不运行第二个 Trade OS、不读取本地旧数据库，离开公司网络后也不会监听该地址。安装和验收步骤见根目录 `DEPLOYMENT.md`。

代码同步与服务器发布由 commit 驱动：每个 Agent 在自己的 worktree 和任务分支中完成一个逻辑完整的 commit，`auto-publish.sh` 接收 commit 或 branch，先在基于 `origin/main` 的临时 release worktree 中 cherry-pick 并跑完整门禁，再推送 release candidate 到公开的 `Sisyphux/trosa`、读取发布前服务器状态、通过 SSH 发射发布并检查公网健康。调用者的 dirty working tree、index 和未跟踪文件都不参与发布。普通改动不需要人工批准；明确的本地-only 请求和疑似破坏性数据库操作除外。

日常自动入口：

```bash
# 单个任务成果
deploy/cloud/auto-publish.sh --commit abc1234

# 任务分支的全部成果
deploy/cloud/auto-publish.sh --branch fix/modal-exit

# 只构建临时 release worktree 并跑门禁
deploy/cloud/auto-publish.sh --dry-run --commit abc1234
```

发布输入只有 commit 或 branch，不接受文件清单，也不会使用调用者的 index、未暂存改动或未跟踪文件。脚本会在基于 `origin/main` 的临时 worktree 中按顺序 cherry-pick 输入，拒绝运行数据、密钥、本地环境文件和生成运行时；数据库敏感改动由服务器在切换流量前生成并校验权威备份（`backup-remote.sh`），本地归档可选、不再阻塞发布。`--dry-run` 只构建候选并执行本地门禁，不访问服务器、不备份、不推送、不发布。

单独发布一个已推送的 commit 用 `trosa-release publish --commit <sha>`（旧的 `publish-workbench.sh` 已删除）。仓库目前没有 GitHub Actions 或 Webhook 自动部署。

日常操作：

```bash
deploy/cloud/trosa-release status --json
deploy/cloud/status-workbench.sh
deploy/cloud/logs-workbench.sh
deploy/cloud/backup-workbench.sh --cloud-only
deploy/cloud/trosa-release rollback
```

`status-workbench.sh` 会先输出 `TROSA_MANAGER_STATUS` 和
`TROSA_MANAGER_RESOURCE` 两行稳定字段，分别供工作台读取服务可用性、`sela` 同步契约版本以及
CPU、内存、根分区磁盘、负载和运行时间；后面的 systemd、磁盘和日志内容仍用于技术排查。
状态与日志检查都通过 SSH 执行。若 SSH 不可握手，脚本会以退出码 2 明确告诉你“连接没建立”，
不会假装服务器状态正常。

发布前建议先查看状态；发布失败时脚本会自动回退，手动回退使用 `trosa-release rollback`。

`auto-publish.sh` 是日常入口；`release-commit.sh` 负责构建发布候选，
`trosa-release publish` 是服务器统一发布器。`release-pipeline.sh` 是常驻发布流水线
（能力分权）：开发方用 `agent-worktree.sh ship` 把分支写进队列
`trosa-tasks/.ship-queue`，流水线消费该队列、按候选 diff 用 `tools/release_tier.py`
重新计算 T0/T1/T2，再调 `release-commit.sh --pipeline`（强制重跑完整门禁、不读开发方
证据、禁用已验收树复用）发布 T1。`run` 默认 dry-run，`--publish` 才真正发布；T2 停在
`awaiting-approval`，T0 合入 main 不部署。`classify --task <id>` 与 `status` 为只读。
流水线自身不存放凭据。`release-commit.sh` 先在干净临时
worktree 中完成 cherry-pick、回归、只读服务器状态和必要备份，再推送唯一的
release commit，最后调用 `trosa-release publish`。底层发布器让服务器后台任务下载指定 commit 的公开归档，
按阶段执行：fetch → db-plan（显式迁移分类）→ backup（仅有数据库变化时，
服务端本地快照）→ migrate（切换流量之前）→ activate（原子切换 `current`
符号链接并重启）→ health（契约 + 页面 + systemd + 迁移账本 +
release 指针五项深度检查）；失败时按阶段自动回滚代码并写入机器可读结果。

发布机制本身有版本门：`release-commit.sh` 在真正发布前，要求当前执行的发布基础设施
（`deploy/cloud/` 与 `tools/release_baseline.py`）与最新 `origin/main` 一致。调用者
checkout 落后（例如历史 worktree 里残留旧脚本）或脚本被改动时，自动从 `origin/main`
的干净临时 worktree 重新执行同一份正式逻辑；无法同步/读取 `origin/main` 时明确拒绝
（fail closed），绝不把已废弃的发布行为重新带回生产。任务代码不需要等于 main ——
这里检查的只是发布机制来源。

并发发布安全由两层保证：

- **本地**：`release-commit.sh` 用共享 git 目录中的可移植锁（死进程/超时可回收）串行化
  发布候选；推送 `origin/main` 使用 `--force-with-lease`，基线上移时后到者被拒绝，绝不
  覆盖另一个发布的提交。
- **服务器**：`flock` 有界等待串行化所有 release（deploy 与 rollback 共用）；超过等待窗口
  写入明确的 `busy` 终端结果并退出，不静默挂起。切换流量前，`tools/release_baseline.py`
  会证明候选 commit 仍包含当前 production commit（GitHub compare，`ahead`/`identical`
  才允许）；基于旧 production、production commit 未知或 compare 不可用都会 fail closed，
  返回 `refused` 且 production 不变。这样两个从同一旧版本开发的并行任务不会互相覆盖：
  先发布者上线，后发布者必须 `sync` 到最新 `main` 重建，最终 production 同时包含两者。

### 门禁执行结构与已验收树复用

一次完整发布门禁只有一份实现 `deploy/cloud/release-test.sh`（任务区、release 候选、
正式发布共用）。它现在：

- 先做快速检查（Python/JS 语法、`tools/check_migrations.py` 迁移完整性）；
- 然后并行三条互不依赖的分支——分支 A：隔离 SQLite 的 Python 回归（失败重跑一次）；
  分支 B：真实 PostgreSQL rehearsal → 真实 Chromium 页面验收；分支 C：浏览器扩展回归。
  输出按分支分组打印，任一支失败都让整道门禁失败；`trap` 统一回收临时资源与
  rehearsal 服务。
- 浏览器验收用 `browser-extension/package-lock.json` 锁定的 Playwright 无头
  Chromium（`tools/run_browser_acceptance.cjs` 驱动 `tools/browser_acceptance.js` /
  `tools/inbox_browser_acceptance.js`），每次运行自己启动一个无头浏览器，不依赖
  开发机上的桌面浏览器；缺 Playwright 一律硬失败，绝不标记为 SKIP。每次运行生成
  唯一 `RUN_ID`（写进验收记录的唯一标记），并只在浏览器阶段持一把机器级锁
  （`deploy/cloud/lib-release-lock.sh`，共享 git 目录），同机并发门禁串行通过。
  驱动按故障类型返回退出码：`21` 基础设施故障、`20` 断言/产品失败。`release-test.sh`
  **只对基础设施故障重跑一次并记入 flake 台账（`trosa-tasks/.flake-events.log`），
  断言失败绝不重跑**，避免把真实缺陷洗成绿；失败时打印页面状态并把截图/状态 JSON
  落到 `trosa-tasks/browser-artifacts/`。
- 一条门禁只起停一次 PostgreSQL rehearsal：`release-test.sh` 选定唯一端口并拥有
  服务生命周期，`tools/browser_acceptance.sh` 被门禁调用时以
  `TROSA_BROWSER_ACCEPTANCE_REUSE_REHEARSAL=1` 复用同一服务/连接，只重载确定性
  fixture（集成测试会改动数据）；该脚本仍可独立运行。

`agent-worktree.sh test --task` 在完整门禁通过且工作树完全干净时，把验收对象登记进
共享账本 `trosa-tasks/.verified-trees`（共享 git 目录，只登记 `result=ok` 且 key
完整的记录），并记录 `verified_tree = HEAD^{tree}`。身份由四项锚定：候选树 tree
hash、门禁实现哈希（运行门禁那份 `deploy/cloud/` + 候选树 `tools/`）、外部输入哈希
（`migrations/` 目录 + 门禁固定测试环境）、基线（`origin/main` sha）。
证据文件 `trosa-tasks/<id>.verify.log` 按 commit **追加**（每次运行追加一段
`# --- run … result=ok|failed ---`），不再整文件覆盖：任务已 `landed` 后重跑门禁只
追加证据、不改写已发布的验证结论。

`release-commit.sh` cherry-pick 后计算候选身份：命中且四项一致时跳过全量门禁，但仍做
快速语法/迁移完整性检查，并打印 `gate reused for tree=<hash>`；未命中走原全量门禁。
树内容、门禁实现、外部输入或基线任一变化（含解析失败/账本缺失/读不到 `origin/main`）
都 fail closed 回到全量。复用按对象身份而非任务目录，release 侧不读取任务工作区路径。
批量发布时每个 `--commit` 输入与 `--branch` 使用同一完成证据判定（`verify_result=ok`、
证据对应该 commit、且已包含最新 `origin/main`），找不到证据即拒绝。

服务器发布锁保证同一时间只有一个 release 在执行；同一 release 重复执行是
幂等的（已是生产版本且健康时直接返回 success）。release id 与 commit 一一绑定，
append-only `.release-ledger.jsonl` 记录每个终端结果；state / result / manifest /
health / migration / backup 全部原子写入，发布中断不会留下半写状态。

## 发布结果状态机与网络异常语义

`publish` / `rollback` 的每个结果都带 `TROSA_RELEASE_STATE` 行，只有五种状态：

- `submitted`：发射命令已在服务器上执行并返回 `launched`（SSH 会话已建立），结果未确认；
- `checking`：正在轮询服务器上的结果文件；
- `success`：已确认成功；
- `failed`：已确认失败/被拒（`refused` / `rolled_back` / `rollback_failed` / `busy`
  归入 failed 家族，但各自仍保留 `TROSA_RELEASE_*` 细分行与退出码）；
- `unknown`：无法确定（SSH 断开、轮询失败、超时）。**unknown 不是发布失败**：production
  状态未知，可能已变更；不要假定未变更，也不要盲目改代码。

网络故障下的行为约定：

1. SSH 会话建立后结果未知（传输层 exit 124）→ 客户端报告 `TROSA_RELEASE_STATE unknown`
   （exit 5），绝不报告"发射失败、production 未变更"；
2. 只有 SSH 会话根本没建立（传输层 exit 2：主机解析失败、连接被拒、认证失败、主机指纹
   不符）时才报告"production 未变更，可直接重跑"；
3. 恢复查询：`deploy/cloud/trosa-release check --release-id <ID> [--json]` 按该
   release 只读读取服务器上的 `DEPLOY_RESULT.json`，归一输出上述状态；无结果时
   返回 unknown（可能仍在 fetch 阶段、发射未被接受或已清理），稍后重跑 check 或
   幂等重跑同一 publish；
4. SSH 连接本身不做自动重试；轮询是只读 `cat`，任何一次失败只意味着“这次没问到”；
5. GitHub compare API（baseline 门）瞬态故障（网络错误 / 5xx / 429）默认重试
   2 次（线性退避）后仍 fail closed；`TRADE_OS_GITHUB_TOKEN` 或 `GITHUB_TOKEN`
   可提高限额（可在服务器 `/etc/trade-os/trade-os.env` 配置，经
   `load_production_env` 传入 guard）。

`deploy/cloud/backup-remote.sh` 在服务器上调用 PostgreSQL 生产目录的 verified logical dump，
核对 SHA-256 与 `pg_restore --list` 可恢复性，并保留在 `/var/lib/trade-os/release-backups/<id>/`。
配置 `TRADE_OS_BACKUP_MIRROR_URI` 与自校验的 `TRADE_OS_BACKUP_UPLOAD_CMD` 后会额外镜像到离机存储；
镜像必须回传匹配的 `size=` 与 `sha256=`，否则判备份校验失败。当前服务器未配置离机镜像。

`backup-workbench.sh` 在此之上可选地把数据库 dump、客户附件和 manifest 打包经 `scp` 下载到 Mac 的
`~/Library/Application Support/trosa/backups/`，核对 bundle SHA-256 后保留最近 14 天的归档。
`--cloud-only` 只生成校验服务器备份不下载；`--download=never|auto|require` 控制本地归档。
下载失败（scp）只影响本地副本，退出码 20 并打印
`TROSA_LOCAL_ARCHIVE local_download_failed`，与备份失败（10/11/12）严格区分。
它不创建服务器系统盘快照；系统盘级灾难恢复需要另外配置云快照或重建服务器。应用在
PostgreSQL 模式下不会把旧 SQLite 目录伪装成备份，也不会通过备份 API 恢复 SQLite 文件。

## 私有浏览器桌面

服务器还运行一个独立的轻量 XFCE 桌面，供必要时在浏览器中直接操作 Ubuntu。它使用
`trosa-desktop` 无 sudo 权限账户，与 Trosa 的 `tradeos` 数据账户隔离；TigerVNC 与 noVNC
只监听服务器 `127.0.0.1:5901/6080`，没有服务器公网端口、UFW 规则或 Cloudflare Tunnel 路由。

从 Mac 通过 `trosa-desktop` SSH 主机别名打开本机隧道，再访问
`http://127.0.0.1:6080/vnc.html`。服务器工作台已将此动作做成“打开服务器桌面”。服务文件、
停用方式和验收要求见 [desktop/README.md](desktop/README.md)。

数据库仍是单写入源。不要在 Mac 上重新启动旧的正式应用并通过同一个域名使用；当前 Mac 的三个旧 LaunchAgent 已禁用但文件保留。如需回退到 Mac，先停止服务器上的 Tunnel，再执行：

```bash
USER_ID=$(id -u)
for label in com.tradeos.app com.tradeos.tunnel com.tradeos.health; do
  launchctl enable "gui/$USER_ID/$label"
  launchctl bootstrap "gui/$USER_ID" "$HOME/Library/LaunchAgents/$label.plist"
done
```

## 多 Agent 任务隔离

每个 Agent 都在一个独立 worktree 和任务分支中工作（`agent/<id>`，目录默认在
仓库同级的 `trosa-worktrees/`，`workbench.env` 从不复制进隔离区）。主工作区只做
集成、验收和发布，不直接写业务代码；完整规则见根目录
[`MULTI_AGENT_WORKFLOW.md`](../../MULTI_AGENT_WORKFLOW.md)。

```bash
deploy/cloud/agent-worktree.sh status                 # 判断当前环境 / 任务归属
deploy/cloud/agent-worktree.sh preflight              # 体检：主区脏文件、迁移编号冲突
deploy/cloud/agent-worktree.sh start --task <id> \
  --owner <name> --goal "..." --scope "..."           # 合并 guard+status+preflight+create
deploy/cloud/agent-worktree.sh create --task <id> \
  --owner <name> --goal "..." --scope "..."           # 只建隔离区（默认不预留迁移号）
deploy/cloud/agent-worktree.sh adopt --task <id>      # 把主区在途改动整体搬进隔离区
deploy/cloud/agent-worktree.sh reserve-migration \
  --task <id>                                         # 懒预留迁移号（真要写迁移时才用）
deploy/cloud/agent-worktree.sh test --task <id>       # 隔离数据目录跑完整回归（含扩展测试）
deploy/cloud/agent-worktree.sh ship --task <id>       # 交付：同步 + 快速门禁 + 登记发布队列
deploy/cloud/agent-worktree.sh sync --task <id>       # 变基到最新 main
deploy/cloud/agent-worktree.sh publish --task <id>    # 发布任务分支的已提交成果
deploy/cloud/agent-worktree.sh remove --task <id>     # 回收（默认保留分支；见下）
```

任务清单（负责人 / 目标 / 修改范围 / 预留迁移编号）存放在共享 git 目录
`trosa-tasks/<id>.json`，不进入版本库。`ship` 只做开发方该做的事（同步、快速门禁、
登记到 `trosa-tasks/.ship-queue` 并把状态置为 `shipped`），不自己发布；完整门禁由
发布侧重跑。发布要求任务 worktree 完全干净
（包含没有未跟踪文件）；随后在基于 `origin/main` 的临时 release worktree 中
cherry-pick 任务 commit，跑同一份 `release-test.sh`，冲突则停止且不修改调用者
工作区。主 worktree 可以继续有其他 Agent 的在途改动。完整说明见
`agent-worktree.sh --help` 与脚本头注释。
