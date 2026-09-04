# PostgreSQL 最终切换清单

状态：`CUTOVER_COMPLETED`（2026-09-03，Asia/Shanghai）

切换批次：`20260903T053710Z`

本清单记录 2026-09-03（Asia/Shanghai）完成的 PostgreSQL 生产切换与验收。当前生产写入路径已切换为 Trosa PostgreSQL 与 sela PostgreSQL；旧 SQLite/JSON 运行素材仍保留在回滚边界内。

## 已通过的上线闸门

- Trosa 生产服务：`trade-os.service` 已启用且运行中，当前 release 为 `/opt/trade-os/releases/20260903041038`；进程环境已确认 `CRM_ENV=production`、`CRM_DB_PATH=/var/lib/trade-os`、`TRADE_OS_DATA_BACKEND=postgres`、`TRADE_OS_DATABASE_URL=postgresql://tradeos_app@127.0.0.1:5432/tradeos` 与权限 600 的 `PGPASSFILE`。
- Trosa 最终 SQLite 快照：`/var/lib/trade-os/backups/2026-09-03/133856_037053`。动态闭集包含 `system.db` 与 6 个注册用户库，7/7 完整性通过；该快照保留用于回滚。
- sela 最终源快照：`/Users/luoxin/Library/Application Support/Sela/postgres-source-snapshots/20260903T053710Z`。候选 1,032 条、反馈 3,049 条、活动 6,196 行（SQLite `integrity_check=ok`）、Run manifest 352 份；冻结快照与最终 PostgreSQL 导入源一致。
- PostgreSQL 目标：`postgres:17.11`，仅监听 ECS `127.0.0.1:5432`，容器 healthy；6 个迁移已应用，迁移文件哈希与 `audit.schema_migrations` 全部一致。
- 最终导入报告：`/opt/trade-os-postgres/reports/import-report-cutover-20260903T052117Z.json`，`result=passed`、`issues=[]`；最终 sela 活动快照哈希为 `84d34adf5cb56b3f48d39ccc5a33ac1a07c3d6362089ae2ff8d6de434b31ed46`。候选库随后从清洁 dump 恢复并完成 restore-check，避免重复导入产生额外审计行。
- 目标计数：`core.companies=1817`、`core.people=1447`、`core.contact_methods=1785`、`trosa.accounts=1334`、`trosa.tasks=2818`、`trosa.timeline_events=3826`、`trosa.outreach_messages=483`、`sela.prospects=1032`、`sela.prospect_events=3049`、`sela.run_activity_events=6196`、`sela.search_memory_entries=532`、`audit.legacy_records=37418`。
- 关系闸门：孤儿引用 `0`、重复 primary domain `0`、迁移问题 `0`、残留 restore-check 数据库 `0`；共享活跃公司 `541`。
- 应用演练：候选 sela 的 `/api/health`、`/api/candidates`、`/api/activity`、`/api/home`、`/api/run/status` 全部 HTTP 200，并完成 PostgreSQL 写入—读取—精确清理；Trosa 候选入口完成 ping、登录、健康检查、客户读取和兼容写入—读取—清理。未发送测试邮件。
- 备份与恢复：最终 dump 为 `/opt/trade-os-postgres/backups/tradeos-20260903T052710Z.dump`，独立本机副本为 `/Users/luoxin/Library/Application Support/Sela/postgres-backups/tradeos-20260903T052710Z.dump`，SHA-256 为 `0e14810dfbe1d71a8aca9f32ac1b49484de7e131ec276c3000f588bf91af20f7`；远端与本机字节一致，`restore-check=passed`，权限为 600。每日备份 timer 已启用且运行中。
- sela 生产服务：`com.luoxin.sela.local` LaunchAgent 已 bootstrap 且 running，进程环境确认 `SELA_DATA_BACKEND=postgres`、本地 SSH tunnel DSN 与权限 600 的 `PGPASSFILE`；PostgreSQL tunnel LaunchAgent running，自动重连逻辑已安装。`/api/health`、`/api/candidates`、`/api/activity`、`/api/home`、`/api/run/status` 全部 HTTP 200，读写烟雾标记已精确清理，v2 mode 仍为 off。
- 公网验收：ECS `cloudflared.service` 已 enabled/active，systemd 配置为 `Restart=always`；在发现 Cloudflare 1033 时重新拉起后，Tunnel 注册连接，`https://app.trosa.space/api/network/ping` 已恢复 HTTP 200。1033 是 Tunnel 可达性症状，不是 PostgreSQL 数据错误。

## 已执行的生产切换

1. 冻结并核对最终 sela 与 Trosa 快照，保留旧 SQLite/JSON 运行素材。
2. 启用 ECS PostgreSQL systemd drop-in，启动 PostgreSQL-backed Trosa，核验进程环境、本机 ping 与公网健康接口。
3. bootstrap 自动重连的 PostgreSQL SSH tunnel，加载 PostgreSQL-backed sela LaunchAgent。
4. 完成五个只读核心接口和一次 PostgreSQL 写入—读取—精确清理验收；未发送测试邮件。
5. 保留旧 release、旧 SQLite 快照、旧 plist 与 PostgreSQL dump；观察期内不删除任何回滚材料。

## 回滚边界

若后续观察期内任一 post-cutover check 失败：先停止 PostgreSQL-backed writer 和 PostgreSQL 版 sela，隔离本次 drop-in/运行 plist，恢复原 `com.luoxin.sela.local` plist 与现有 `/opt/trade-os/current` SQLite service；PostgreSQL 容器、dump、导入报告和 audit 历史保留用于诊断。任何时刻不允许 SQLite 与 PostgreSQL 两套 writer 同时运行。
