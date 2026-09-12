# Trosa PostgreSQL 正式运行契约

> 当前状态：Trosa 的正式业务模型与写入边界已经收敛到 PostgreSQL。本文件是
> 当前运行契约和恢复规则，不再是“SQLite 生产路径 → PostgreSQL”的切换清单。
> 截至 2026-09-12，ECS release `auto-20260912081434-7535b79` 已通过远端健康响应、
> 生产 `verify_schema` 和 Sela readiness 验收；后续发布仍以线上证据为准。

## 当前唯一正式路径

```text
浏览器 / 浏览器扩展 / sela Gateway
        ↓ HTTPS（受限 API）
ECS trade-os.service → serve.py → Trosa Flask
        ↓
ECS PostgreSQL（唯一正式业务 writer）
        +── /var/lib/trade-os/uploads、导入来源、回滚材料
```

- `serve.py` 是唯一正式启动入口，要求 `CRM_ENV=production`、
  `TRADE_OS_DATA_BACKEND=postgres`、非空 `TRADE_OS_DATABASE_URL`，并在迁移、调度器
  和监听端口前拒绝不完整配置。
- `/api/network/ping` 只有同时满足 `status=ok`、`backend=postgresql`、
  `formal_runtime=true` 和 `runtime_contract=trosa-postgresql-v1` 才能作为正式健康状态。
- `app.py`、`desktop.py` 和根目录启动器不再是正式业务启动入口。SQLite 只允许用于
  显式隔离开发、历史导入/演练和明确批准的恢复材料；它不能与正式 PostgreSQL
  writer 并行运行。
- sela 只通过受限 HTTPS Gateway 读取和提交业务动作；不直连 PostgreSQL，不保存
  客户、联系人、阶段、任务或人工判断副本。

## 安全开发与验证

新开发者先阅读 `README.md` 的“运行契约”和 `docs/SYSTEM_STATE.md`，再按变更类型
选择入口：

```bash
# 完整 PostgreSQL 演练：独立 loopback 集群、独立数据库，不触碰 ECS
python3 tools/postgres_rehearsal.py test

# 常规回归（测试自行使用隔离 CRM_DB_PATH）
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m py_compile app.py db.py scheduler.py serve.py serve_rehearsal.py
node --check app/static/app.js
(cd browser-extension && npm test)
```

只有需要旧 SQLite 形状回归时，才显式运行：

```bash
CRM_ENV=development TRADE_OS_DEV_SQLITE=1 .venv/bin/python desktop.py
```

该命令是隔离测试工具，不是正式工作台，也不应连接正式数据目录。

## 发布前后的健康门

发布脚本必须验证同一个版本的：

1. PostgreSQL 迁移账本和 schema contract；
2. 应用 `/api/network/ping` 的三个正式契约字段；
3. Trosa 核心页面、写入、Inbox、Today、搜索、附件和恢复边界；
4. sela 的 `runtime_contract=trosa-postgresql-v1`、`backend=postgresql` 和
   `formal_runtime=true`。

`deploy/cloud/status-remote.sh` 与 `publish-remote.sh` 已把上述 ping 字段作为健康门。
若远端返回旧版/SQLite/缺字段响应，应视为未就绪并停止后续业务写入，而不是降级到
本地 SQLite。

## 恢复规则

- 正式备份是 PostgreSQL logical dump + 客户附件 bundle + manifest/哈希，并单独做
  restore-check。
- 需要恢复时先停止原主机的应用与 Tunnel，再恢复 PostgreSQL 和附件，最后只启动一
  个 `serve.py` writer。
- `CRM_DB_PATH` 中的 SQLite 只能作为明确批准的历史恢复材料；恢复完成后不得让它
  重新成为日常业务源。
- 禁止同时启动 SQLite writer 和 PostgreSQL writer。保留旧 release、dump、导入报告
  和审计账本供诊断，不把兼容视图当作新的业务模型。

## 历史记录

此前的实际导入与切换证据保留在 `POSTGRESQL_FINAL_CUTOVER_CHECKLIST.md`；它是历史
证据，不是新的启动说明。架构变化记录在 `docs/CHANGELOG.md`，当前事实以
`docs/SYSTEM_STATE.md` 为准。
