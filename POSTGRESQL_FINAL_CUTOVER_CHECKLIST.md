# PostgreSQL 最终切换清单

状态：`READY_FOR_CONFIRMATION`（尚未切换生产）

本清单记录 2026-09-03（Asia/Shanghai）完成的最终核验。当前生产写入路径仍是 Trosa SQLite 与 sela JSON/SQLite；PostgreSQL 目标库只承载候选数据和演练流量。

## 已通过的上线闸门

- Trosa 生产服务：`trade-os.service` 已启用且运行中，当前 release 为 `/opt/trade-os/releases/20260903041038`；生产环境仍只有 `CRM_ENV=production`、`CRM_DB_PATH=/var/lib/trade-os`，未设置 PostgreSQL 路由变量。
- Trosa 最终 SQLite 快照：`/var/lib/trade-os/backups/2026-09-03/124036_079533`。动态闭集包含 `system.db` 与 6 个注册用户库，7/7 完整性通过；7 个数据库哈希均与最终导入报告一致。
- sela 最终源快照：`/Users/luoxin/Library/Application Support/Sela/postgres-source-snapshots/20260903T042048Z`。候选 1,032 条、反馈 3,049 条、活动 6,196 行（SQLite `integrity_check=ok`）、Run manifest 352 份；当前运行中的源与快照的 JSON、活动逻辑行和 Run manifest 均一致。
- PostgreSQL 目标：`postgres:17.11`，仅监听 ECS `127.0.0.1:5432`，容器 healthy；6 个迁移已应用，迁移文件哈希与 `audit.schema_migrations` 全部一致。
- 最终导入报告：`/opt/trade-os-postgres/reports/import-report-final-20260903T042048Z.json`，`result=passed`、`issues=0`；最终 sela 活动快照哈希为 `84d34adf5cb56b3f48d39ccc5a33ac1a07c3d6362089ae2ff8d6de434b31ed46`。
- 目标计数：`core.companies=1817`、`core.people=1447`、`core.contact_methods=1785`、`trosa.accounts=1334`、`trosa.tasks=2818`、`trosa.timeline_events=3826`、`trosa.outreach_messages=483`、`sela.prospects=1032`、`sela.prospect_events=3049`、`sela.run_activity_events=6196`、`sela.search_memory_entries=532`、`audit.legacy_records=37418`。
- 关系闸门：孤儿引用 `0`、重复 primary domain `0`、迁移问题 `0`、残留 restore-check 数据库 `0`；共享活跃公司 `541`。
- 应用演练：候选 sela 的 `/api/health`、`/api/candidates`、`/api/activity`、`/api/home`、`/api/run/status` 全部 HTTP 200，并完成 PostgreSQL 写入—读取—精确清理；Trosa 候选入口完成 ping、登录、健康检查、客户读取和兼容写入—读取—清理。未发送测试邮件。
- 备份与恢复：最终 dump 为 `/opt/trade-os-postgres/backups/tradeos-20260903T044706Z.dump`，独立本机副本为 `/Users/luoxin/Library/Application Support/sela/postgres-backups/tradeos-20260903T044706Z.dump`，SHA-256 为 `6ed207783be18178017019ca9579c80c0221a07b37abaf63d0627a4a7139affe`；远端与本机字节一致，`restore-check=passed`，权限为 600。每日备份 timer 已启用且运行中。
- sela 切换素材：专用 venv 可导入 `psycopg 3.2.10`，`PGPASSFILE` 为 600；PostgreSQL 应用 plist 与 tunnel plist 已通过 `plutil -lint`，但尚未 bootstrap。当前 sela LaunchAgent 仍为 `com.luoxin.sela.local` 的 JSON/SQLite 实例，ready=true、v2 mode=off。

## 唯一确认点

当前停在这里。只有用户明确确认“执行生产切换”后，才执行下面的动作；确认前不停止现有 writer、不加载 tunnel、不修改任何生产服务环境变量。

## 确认后的执行顺序

1. 停止现有 sela LaunchAgent，冻结本地 JSON/SQLite 写入；立即生成并核对最终 sela 快照。
2. 停止 `trade-os.service`，冻结 Trosa SQLite 写入；立即生成并核对最终 7 库快照。若任一最终快照哈希改变，先重新导入候选库并重新生成/恢复校验 dump。
3. 在 ECS 创建权限 600 的 PostgreSQL pgpass 文件和 systemd drop-in，仅加入 `TRADE_OS_DATA_BACKEND=postgres`、`TRADE_OS_DATABASE_URL`、`PGPASSFILE`；daemon-reload 后启动 Trosa，并核验进程环境、`/api/network/ping` 和认证后的核心读写路由。
4. bootstrap PostgreSQL SSH tunnel，再加载 PostgreSQL 版 sela LaunchAgent；核验 `/api/health`、`/api/candidates`、`/api/activity`、`/api/home`、`/api/run/status` 与一次候选库写入—读取—清理。
5. 观察窗口内不删除旧 release、旧 SQLite 快照、旧 plist 或 PostgreSQL dump；只有所有 post-cutover checks 通过后才结束切换。

## 回滚边界

若任一 post-cutover check 失败：先停止 PostgreSQL-backed writer 和 PostgreSQL 版 sela，移除本次 drop-in/tunnel，恢复原 `com.luoxin.sela.local` plist 与现有 `/opt/trade-os/current` SQLite service；PostgreSQL 容器、dump、导入报告和 audit 历史保留用于诊断。任何时刻不允许 SQLite 与 PostgreSQL 两套 writer 同时运行。
