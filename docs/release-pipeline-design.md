# 发布流水线改造设计说明（阶段 1）

> 目标：把「开发完写好 → 人工/会话切换角色 → 才能发布」改成「开发方只管提交、常驻流水线按能力自动同步→门禁→发布→健康检查→失败回滚」。
> 本文只产出设计与选择理由，不含实现。提交给发布方确认后再进入阶段 2。

- 撰写日期：2026-09-30
- 基线（写作时）：`main` = `eb4e947`
- 相关文件：`AGENTS.md`、`MULTI_AGENT_WORKFLOW.md`、`deploy/cloud/{release-env.sh,agent-worktree.sh,release-commit.sh,release-remote.sh,release-test.sh,lib-release-gate.sh,lib-release-lock.sh,trosa-release,auto-publish.sh,cloud-assistant.py}`

---

## 0. 一句话结论

真正的发布凭据**不是** `~/.config/trosa/workbench.env`（它只是路由元数据），而是 `~/.workbench/config.json` 里的阿里云 RAM AK。两者都在开发机同一个登录用户家目录下，因此**单机同用户/同管理员**的前提下，环境变量和文件权限都做不出硬边界。要满足「开发会话读凭据必失败、且失败原因是权限」这条验收，凭据必须离开开发会话所在的信任域：要么放进 GitHub Environment（方案 A），要么给发布角色一个开发用户拿不到文件的独立账户（方案 B，且要求开发会话不是同级管理员）。

---

## 1. 实测现状（2026-09-30，本机）

### 1.1 命令耗时

| 环节 | 耗时 |
|---|---|
| guard / status | 0.1–0.2 s |
| create | 0.5 s |
| preflight | 1.9 s |
| sync（变基 + 迁移编号消解） | 1.8 s |
| `test --task` 完整门禁 | 1 分 53 秒 |
| 其中 Python 回归（577 用例） | 约 111 s（关键路径） |
| 其中 PostgreSQL rehearsal（38 用例） | 约 7 s |
| 其中扩展回归 | 几秒 |

命令本身都快；真正的等待发生在**交接**上，不在命令上。

### 1.2 观察到的问题

1. **交付积压**：22 个 worktree、20 个 active 任务、仅 3 个 landed。`audit-remediation-20260920`、`change-ownership-ledger`、`room-*-0929`、`ui-daylight-*` 等都是「门禁 ok、尚未发布」。绿了却没人发布，是交接瓶颈的典型症状。
2. **`create` 无条件预留迁移号**：建一个不涉及数据库的探针任务也会烧掉一个编号，计数器 `.migration-counter` 单调不回收（当前 `0111`）。虽然已有 `--no-reserve-migration` 选项，但默认仍是取号。
3. **纯文档改动也要过完整门禁（含浏览器验收）**：docs-only 改动会被无关的浏览器 flake 挡住。
4. **两遍门禁**：`test --task` 一遍，release 候选一遍；复用只在 tree、门禁实现、迁移、`origin/main` 四项都不变时成立，`main` 每前进一格就失效。
5. **浏览器验收间歇性失败（根因未查清）**：在「main + 一个 docs 文件」的分支上，`acceptance-core-workflow` 连续失败两次且错误不同——一次 `#ledgerScroll ... Browser acceptance customer reply` 等待超时（41 s），一次 `the complete modal did not put the caret in the capture textarea`（8.8 s）。两次都在 `sync` 变基到含 `41bd347`（Inbox 专项验收上传等待修复，已 landed）与 `8744715`（complete-modal-polish：打开即输入，已 landed）之后的基线上。第二个错误恰好落在 `8744715` 改动的行为上，**不是** `inbox-acceptance-flake-0930` 修掉的那个问题（它修的是 `tools/inbox_browser_acceptance.js` 的 Inbox 专项验收）。Python 分支有「失败重跑一次」，浏览器分支**没有**，也没有 flake 记录。

### 1.3 没有做的事

没有真实执行 publish（会改 ECS），没有走通 dry-run（门禁红，走不到），没有验证 dev 角色调用 publish 的拒绝分支（该命令被拒绝执行），这一条只依据文档和代码阅读。

---

## 2. 目标与非目标

**目标**

- 开发方只拥有「提交」能力：改代码、跑快速检查、把分支和证据交出去；物理上拿不到发布凭据。
- 发布方是常驻的、持凭据的自动流水线：看到合格交付就自动 同步 → 全量门禁 → 发布 → 健康检查 → 失败自动回滚，无人需要切换身份。
- 安全不降低：现有每一道保护都保留，判定改由流水线自己重新计算，不信任开发方声明。
- 用**能力**（谁持有什么凭据/权限）划边界，不用**身份标签**（环境变量里写自己是谁）。

**非目标**

- 不新增第二份门禁实现（`release-test.sh` 仍是唯一实现）。
- 不放松 production 基线门、切换前备份、ECS 深度健康检查、发布串行锁、`--force-with-lease`。
- 不触碰 ECS 正式数据；测试仍用独立 `CRM_DB_PATH`。
- 不恢复被冻结的功能，不做大规模重构。

---

## 3. 能力模型

| 能力 | 持有者 | 能做什么 | 拿不到什么 |
|---|---|---|---|
| `propose` | 开发方（本机开发会话） | create/adopt、改代码、commit、`sync`、`test --quick`、`ship` | 发布凭据（AK / 路由）、不能 push `main` |
| `release` | 常驻流水线 | 消费提交、rebase、跑全量门禁、发布、回滚、写证据 | ——（这本身就是持凭据方） |
| `approve` | 人 | 仅对 T2 类变更放行 | ——（是一个动作，不是环境变量） |

关键变化：`TRADE_OS_AGENT_ROLE` 从「安全边界」降级为「同机护栏 + 弃用垫片」。它不再决定谁能发布，能力只由**是否持有凭据**决定。

---

## 4. 凭据边界（本设计的核心）

### 4.1 发布凭据到底有哪些

现有链路 `trosa-release → cloud-assistant.py → 阿里云 Cloud Assistant RunCommand` 需要：

1. `~/.workbench/config.json`（或 `$TRADE_OS_WORKBENCH_CONFIG`）：`profiles[name].{access_key_id,access_key_secret}`，`mode` 必须为 `AK`。**这是真正的发布凭据**——拿到它就能对 ECS 下发命令。
2. `~/.config/trosa/workbench.env`：ECS region、instance id、远端路径、公网 URL、服务名等**路由元数据**（`workbench.env.example` 里没有任何密钥）。它是「凭据的一半」：只有 AK 没有路由不知道发给谁，只有路由没有 AK 签不了名。

`release-env.sh` 的头注释自己承认：「同机同用户的进程无法用环境变量做硬隔离。真正的边界是文件权限——发布配置放在仓库外并设为 600」。

**因此硬边界必须同时覆盖上述两个文件，原任务描述只点了 `workbench.env`，AK 文件必须一并纳入。**

### 4.2 为什么「本机 + 同一个管理员用户」做不出硬边界

macOS 上日常登录用户通常是管理员，可以 `sudo -u <任何用户>` 而无需该用户密码，也能 `sudo` 读任何文件。所以：

- 换一个「独立 macOS 用户」并把配置设成 0600，**只对非管理员开发会话有效**；开发会话若是管理员，仍然能读。
- 也就是说，方案 B 只有在「开发会话不是管理员、且没有免密 sudo」时才是硬边界；否则只是「不顺手」的软边界。

这条约束直接决定了方案选择（见第 5 节）。

### 4.3 验收映射

任务验收要求「开发会话执行 `cat ~/.config/trosa/workbench.env` 与调用发布必须失败，且失败原因来自权限，而非环境变量」。要满足「原因是权限」，必须满足其一：

- 凭据根本不在开发机（方案 A：在 GitHub Environment）；或
- 开发机会话是非管理员账户，凭据 0600 归发布账户所有（方案 B 的强化形态）。

---

## 5. 方案对比与选择

### 5.1 方案 A：GitHub Actions + GitHub Environment

- 发布密钥放 GitHub Environment secrets；T2 用 Environment 的 required reviewers 作为 `approve`。凭据完全不在开发机上。
- 流水线触发：push `agent/*` 或开 PR。
- 门禁需要 PostgreSQL 与 Chromium：`ubuntu-latest` runner 上可用 `services: postgres` 或 apt 安装，Chromium 用 `npx playwright install --with-deps chromium`。
- 需要写 `main` 的 token（`--force-with-lease` push），以及调阿里云 RunCommand 的 AK。

**优点**：唯一能在「开发会话是管理员」时仍成立的硬边界；T2 审批有官方机制；多机/多人天然适用。

**代价/前提**：本仓库目前**没有任何 CI**；门禁是本机 macOS 脚本（`.venv`、`browser-extension/node_modules`、Playwright 缓存、`tools/postgres_rehearsal.py` 自管 PG 集群、4 锚点复用身份假设本地路径）。搬到 Actions 是大工程，需要先在 runner 上「跑得起来、跑得一样」——**这一步尚未验证**。且配置 GitHub secrets/Environment/仓库设置属于对外可见操作，需先经用户批准。

### 5.2 方案 B：本机 launchd 常驻 runner（独立账户）

- 以另一个 macOS 用户运行 launchd 常驻进程；`workbench.env` 与 `~/.workbench/config.json` 0600 归该用户所有。
- 队列用仓库外目录或 git ref（如 `.git/trosa-queue/` 或 `refs/trosa/queue/*`）。
- 复用现有全部本地机制（单一门禁实现、4 锚点复用、`trosa-tasks` 证据、`--force-with-lease`、ECS 基线门/回滚）。

**优点**：与现有全本地 git 架构最契合，改动最小；不需要任何对外可见操作；「开发会话不能读凭据」在这台机器上可直接验证。延迟低（无网络往返）。

**代价/前提**：只有在**开发会话不是同级管理员**时才是硬边界（见 4.2）。要么把开发会话降为非管理员用户，要么接受它只是软边界。单机、单设备场景适用；多设备要各自部署。

### 5.3 方案 C：ECS 侧拉取式 runner

- 在 ECS 上拉取并跑门禁。**要评估 ECS 能否承担门禁**：需要 Python + PostgreSQL + Chromium，与正式服务同机，资源竞争和误伤正式数据的风险高。

**结论**：不建议。生产主机不应承担 111 s 的 Python 回归 + Chromium 验收；隔离成本大于收益。

### 5.4 决策矩阵

| 维度 | A：GitHub Actions | B：本机 launchd 独立账户 | C：ECS 拉取 |
|---|---|---|---|
| 对管理员开发会话是否硬边界 | ✅ 是（凭据离机） | ⚠️ 仅当开发会话非管理员 | ✅ 是 |
| 与现有本地架构契合 | 低（需重搭门禁运行环境） | 高（基本复用） | 中 |
| 需要对外可见操作 | 是（GitHub secrets/设置） | 否 | 否（但有生产风险） |
| 门禁可行性 | 未验证（runner 能否跑 PG+Chromium） | 已验证（本机已跑通） | 未验证，风险高 |
| 无密码/无人切换 | ✅ | ✅ | ✅ |
| 多机/多人 | ✅ | ❌（需逐机部署） | ✅ |

### 5.5 推荐

**分两步：**

1. **先做 B（本机 launchd 独立账户）作为落地形态**，因为：
   - 与现有 `deploy/cloud` 全本地 git 机制最兼容，对「必须保留」清单几乎是零风险；
   - 不需要任何对外可见操作，不触碰 GitHub，符合用户「先问再做」的规则；
   - 门禁在本机已被证明可跑通，阶段 4 的 `--dry-run` 全链路最快能落地。
2. **把「开发会话为非管理员用户」作为 B 的硬边界前提写进验收**；若用户不接受改动日常登录账户，则 B 降级为「软边界 + 审计」，并**把 A 升为目标架构**（此时需要用户批准 GitHub 侧配置）。

> B 与 A 的流水线主体（同步 → 门禁 → 发布 → 健康检查 → 回滚 → 证据）是同一套逻辑，只是执行机与凭据存放处不同。因此先落 B、后续迁移 A 不会浪费。

### 5.6 决策记录（2026-09-30 用户确认）

| 决策 | 结论 |
|---|---|
| 流水线形态 | **采用方案 B（本机 launchd 独立账户）**，作为阶段 4 的落地形态。 |
| B 的硬边界前提 | **开发会话改为非管理员 macOS 用户**；否则 B 只是软边界。阶段 4 需先做「开发会话非管理员」+「发布账户 0600 持有凭据」的最小 PoC 并纳入验收。 |
| 凭据边界范围 | **`~/.config/trosa/workbench.env` 与 `~/.workbench/config.json`（阿里云 RAM AK）都必须移出开发会话信任域**。只覆盖前者不算完成（见 4.1）。 |
| 后续目标架构 | 多机/多人时再评估迁移到方案 A；本次不实施 A。 |
| 阶段推进 | 确认后进入阶段 2（先修门禁可信度）。 |

因此第 12 节的阶段 4 按 B 实施；第 14 节的「选 A 还是 B」「是否接受非管理员开发会话」「AK 是否纳入边界」三项已由本表关闭。

---

## 6. 流水线重算契约（不信任开发方证据）

流水线收到分支后**从不信任**开发方交来的 `verify.log`，它自己重新计算：

1. 临时 worktree 里 rebase 到最新 `origin/main`；迁移编号碰撞沿用 `tools/reconcile_migrations.py`。
2. 跑 `release-test.sh` 完整门禁（沿用唯一实现，不新增第二份）。4 锚点复用身份（`tree` / `gate_impl` / `external` / `base`）保持不变，**fail closed**：任一锚点对不上就重跑，绝不跳过。
3. 现有发布逻辑：`release-commit.sh` → `release-remote.sh`，包括 production 基线门、切换前备份校验、ECS 深度健康检查、`--force-with-lease`、发布串行锁——**一行都不放松**。
4. 发布后深度健康检查失败 → **自动回滚**（复用 `release-remote.sh` 内既有的健康失败自动切回 `previous_healthy` 机制，与 `trosa-release rollback`），并把 task 状态标失败、写明原因。
5. 全程写证据：`tree`、门禁结果、`release-id`、是否回滚，沿用 `trosa-tasks/<id>.verify.log` 语义；`landed` 仍是唯一的「完成」。

> 注：需求 3.2.4 的「健康失败自动回滚」在 ECS 侧**已存在**（`release-remote.sh` 的 deploy 在 `wait_healthy` 失败时会自动切回 `$prod_before`）。本次改造的增量是：把「触发发布」从人切换角色改为流水线，并确保回滚结果写回 task 证据。

---

## 7. 风险分级（由流水线按 diff 计算，不由开发方声明）

| 级别 | 触发 | 行为 |
|---|---|---|
| T0 | 只改 `docs/`、`design/`、`*.md`、`tests/` 且不影响运行代码 | 合入 `main`，不部署。是否跑浏览器验收由流水线按实际风险决定并写明理由（`change-ownership-ledger` 的 `--docs-only` 豁免可参考） |
| T1 | 其余常规代码 | 门禁绿 → 自动发布 → 健康检查 → 失败自动回滚 |
| T2 | 触及 `migrations/`、`deploy/`、`serve.py`、`config.py`、备份/恢复/发布脚本、`AGENTS.md` 里的不可逆高风险操作，或任何改动流水线自身的变更 | 门禁绿后**暂停**，等待 `approve`；未放行不发布 |

分级规则本身放在流水线一侧受保护的位置；**改动分级规则/流水线代码永远是 T2**。

---

## 8. 开发方入口收敛

- `start --task <id> ...`：合并 guard + status + preflight + create。迁移号改**懒预留**：新增 `reserve-migration --task <id>`，只有真要写迁移时才取号，`create` 不再默认取号。
- `ship --task <id>`：`sync` → 快速检查（`release-test.sh --quick`）→ 把分支交给流水线（推 `agent/<id>` 或写队列），然后返回。开发方不再等待完整门禁；完整门禁只在流水线跑一遍。需提前看结果的场景保留 `test --task`。
- 现有子命令（`guard/status/preflight/create/adopt/sync/test/gate/evidence/remove`）全部保留，`ship` 只是组合。

---

## 9. 失败模式与处置

| 失败模式 | 现象 | 处置 |
|---|---|---|
| 流水线宕机 | 任务提交后长时间无人认领 | fail closed：不发布任何东西是安全的。流水线写心跳；`status` 显示流水线离线；告警 |
| 队列卡死 | 任务被认领但 runner 中途死亡，锁/租约未释放 | 租约超时自动回收重试；发布按 `release-id + commit` 幂等，重试安全；`lib-release-lock.sh` 的串行锁已有 stale 处理 |
| runner 被入侵 | 攻击者拿到 AK 与 push `main` 能力 | 最小权限：RAM 子账号只给 RunCommand 所需 Action，GitHub token 只给单仓库写权限；环境审批；证据审计；凭据可轮换。ECS 侧 production 基线门（候选必须包含当前 production commit）限制能推的东西；切换前备份 + 自动回滚限制影响 |
| 部分发布（push 了 main，ECS 发布失败） | main 与 production 不一致 | `trosa-release` 状态机 + production 基线门可对账；记录并人工/流水线重试；必要时 `trosa-release rollback` |
| 门禁 flake（浏览器分支） | 偶发红，与改动无关 | 阶段 2：浏览器分支加「失败重跑一次 + 记录为 flake 事件」，**不得静默算通过**；flake 事件可汇总 |

---

## 10. 回滚步骤

1. **自动**：deploy 阶段深度健康检查失败时，`release-remote.sh` 已自动切回 `$previous_healthy`（= `$prod_before`）并重启，重新深健康检查；成功 → `rolled_back`（生产健康），失败 → `rollback_failed`。
2. **手动/流水线**：`trosa-release rollback [--allow-destructive-db]`（`rollback-workbench.sh` 已弃用，仅保留 shim）。
3. **应急**：`deploy/cloud/auto-publish.sh --commit <上一个好 commit>`（需持 `release` 能力）。
4. 回滚后流水线把 `rolled_back` / `rollback_failed` 与原因写入 `trosa-tasks/<id>.verify.log`，并把 task 状态置为失败。
5. 演练（阶段 5）：在隔离环境注入健康检查失败，证明会自动回滚且 production 状态不变。

---

## 11. 兼容与迁移

- 保留 `TRADE_OS_AGENT_ROLE` 一个版本作为**弃用垫片**：映射到能力并输出弃用提示；下一版本删除。
- 现有 `publish --task` 与 `auto-publish.sh --commit/--branch` 继续可用（人工应急发布，需持 `release` 能力），行为不变。
- 同步更新 `MULTI_AGENT_WORKFLOW.md`、`AGENTS.md`；`CHANGELOG.md` 仅当有用户可感知变化才写。
- 删掉「切换角色」的说法。

---

## 12. 分阶段交付（每阶段独立 commit，都要有测试与证据）

1. **设计说明**（本文，不写实现）——对比 A/B/C、选择、凭据边界、失败模式、回滚步骤。等用户确认再进入第 2 阶段。
2. **先修门禁可信度**：定位 `acceptance-core-workflow` 间歇失败根因（两种错误都要解释）；浏览器分支加「失败重跑一次 + flake 事件」，不得静默算通过，flake 要可汇总。注意 `inbox-acceptance-flake-0930` 已 landed，不要重复做；先看 `8744715` 的弹窗改动与 `tools/browser_acceptance.js` 的 `acceptance-core-workflow` 是否有竞态。
3. **入口收敛**：`start` / `ship` / 懒预留迁移号 + 测试。
4. **流水线**：按选定方案实现，先只支持 T1，且先接 `--dry-run` 全链路跑通再开真发布。
5. **分级与自动回滚**：T0/T2、`approve`、回滚演练（隔离环境注入健康检查失败，证明自动回滚且 production 不变）。
6. **积压治理**：给 `list`/`status` 增加「绿了未发布超过 N 天」「过期任务」提示；不自动删除任何 worktree，只提示，由人确认后 `remove`。

---

## 13. 验收映射

| 验收项 | 本设计的落点 |
|---|---|
| 开发会话 `cat workbench.env` 与调用发布必失败，原因是权限 | 方案 A 天然满足；方案 B 需开发会话非管理员（见 4.2/5.5） |
| 一个 T1 任务 `ship` 到 `landed` 无需人工切换 | 第 6 节重算契约 + 第 3 节能力模型 |
| 伪造「门禁 ok」的 `verify.log` 被无视并重跑 | 第 6 节第 1–2 步（流水线自算） |
| 注入健康检查失败自动回滚，`/api/network/ping` 仍 `status=ok, backend=postgresql, formal_runtime=true, trosa-postgresql-v1` | 第 10 节（既有机制 + 阶段 5 演练） |
| T2 未 `approve` 不发布，`approve` 后才发布 | 第 7 节 + 方案 A 的 required reviewers / 方案 B 的人在环 |
| 两任务同时 `ship` 串行，后者基于最新 production | 既有发布串行锁 + production 基线门（保留，不放松） |
| 现有 `tests/` 全通过；新增测试覆盖分级、伪造证据、凭据隔离、回滚 | 阶段 2–5 各自的测试 |

---

## 14. 未验证 / 待确认

**未验证**

- 方案 A 的可行性：GitHub Actions runner 上能否原样跑通本机门禁（自管 PostgreSQL rehearsal 集群、Chromium 验收、4 锚点复用身份），**未验证**。
- 本机 launchd 独立账户方案的权限细节（worktree 归属、git 凭据、push 权限），**未验证**，需在阶段 4 做最小 PoC。
- dev 角色调用 `publish` 的拒绝分支：因命令被拒绝执行，只依据代码/文档阅读，**未实际触发验证**。
- 真实 publish / dry-run 全链路：未执行（会改 ECS；门禁红走不到）。

**待用户确认（已在 5.6 决策记录中关闭）**

1. ~~选 A 还是 B~~ → 采用 **B**（开发会话非管理员为前提）。
2. ~~是否接受「开发会话降为非管理员用户」~~ → **接受**，阶段 4 纳入验收。
3. ~~凭据边界是否覆盖 `~/.workbench/config.json`（AK）~~ → **纳入**。
4. 流水线队列形态（仓库外目录 vs git ref）——**仍待定**，在阶段 4 设计前置确认。
