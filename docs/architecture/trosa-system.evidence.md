# Trosa 系统模型 — 证据索引、置信度与复核记录

场景技能：`system-modeler` ｜ 基础技能：`c4model`（C4 DSL）、`graphviz`（DOT）

## 元信息

| 项 | 值 |
| --- | --- |
| 模型状态 | **当前状态**（current state），不含目标态 |
| 指纹基线 | 2026-09-12 16:28 CST 发布后运行态（commit `0a25827`，release `auto-20260912082853-0a25827`；包含功能收敛 commit `7535b79`） |
| 覆盖范围 | 整个仓库：后端、前端静态资源、浏览器扩展、迁移与数据层、部署与运行环境 |
| 未覆盖 | 列级外键与逐列数据血缘；`0001`–`0029` 全部 DDL 的逐条审计；Cloudflare Access 策略 |

**基线注意事项**

1. **本模型对应已发布版本，不是移动靶**。`app.py` 当前为 15,152 行，所有数字以文末指纹表和
   commit `0a25827` 为准；新增发布后应重新核对远端 SHA 与迁移账本。
2. **远端证据已补齐**。ECS 当前 release 为 `auto-20260912082853-0a25827`；公网 ping
   四字段健康门通过，受控 `verify_schema` 确认 0001–0029、schema contract 和数据完整性。

## 本轮复核记录（哪些结论被推翻或修正）

| # | 上一轮结论 | 复核后的事实 | 处理 |
| --- | --- | --- | --- |
| 1 | 「`db.py` 迁移清单止于 0027，与 `tools/unified_postgres_migration.py` 含 0028 不一致」 | **已修正**：`db.py` 与迁移工具均含 `0028`、`0029`，生产 ledger/schema 也已通过全量校验 | 撤销该发现；后续发布重复 ledger 校验 |
| 2 | 「`app.py` 14,534 行」 | `app.py` 现为 **15,152 行** | 已更新，并改为引用「约 1.5 万行」+ 指纹 |
| 3 | 「路由分布：客户 36、Sela 16、gateway 11…」 | 前缀口径不完整。实测 **31 个 `/api` 前缀 / 154 条 API + 4 条非 API = 158** | 已改为完整分布 |
| 4 | 「客户状态/等级取值未知」 | **等级取值已查明**：`db.py:992` `CUSTOMER_LEVEL_VALUES`，`db.py:1746` 拼进 SQL 强制 | 已补入业务文档；阶段/判词仍未核实 |
| 5 | 「`audit.operation_log_events` 是否由代码写入未确认」 | **已确认写入路径**：`app.py` `_record_operation_log()` 文档写明写入规范审计事实 + 可选旧客户端投影 | 数据拓扑中该节点升级为 confirmed（结构 + 写入路径） |

## 证据类型分布

| 类型 | 代表来源 | 在本模型中的用途 |
| --- | --- | --- |
| code | `app.py`、`trosa_domain.py`、`db.py`、`app/engine.py` | 路由面、业务写入路径、组件导入边 |
| config | `deploy/trade-os.service`、`deploy/cloudflared.service`、`.env.example`、`.gitignore` | 运行单元、环境边界 |
| data | `migrations/0001–0029`、`postgres_schema_contract.py` | 数据拓扑、schema 与视图归属 |
| script | `deploy/cloud/*.sh`、`tools/*.py` | 发布链路、rehearsal 与迁移门禁 |
| document | `AGENTS.md`、`TROSA_MAINTENANCE.md`、`POSTGRESQL_FINAL_CUTOVER_CHECKLIST.md` | 产品边界、已冻结范围、切库完成记录 |
| human-assumption | 本文件「假设」小节 | 显式标注，不混入确认路径 |

## L1 / L2 节点证据

| 节点 | 证据 | 置信度 |
| --- | --- | --- |
| 销售用户（3 人） | `db.py:29-34` 用户注册表；`AGENTS.md`「三人使用」 | high |
| Cloudflare 边缘与 Tunnel | `deploy/cloudflared.service:11`；`deploy/cloudflared-config.yml.example:4-7`（hostname → `http://127.0.0.1:8080`） | high（Tunnel）／**unknown（Access 策略）** |
| Sela 外联代理 | `app.py` `/api/integrations/sela` **16 条**（实测 `grep -c`）；Sela 仓库 `tools/trosa_client.py`、`tools/trosa_first.py` 和 `app.py` 的 HTTPS/outbox 实现 | high（接口与本地实现） |
| Google Gmail API | `gmail_sync.py:46` 只读 scope `gmail.readonly`；`/api/integrations/gmail` **5 条**（实测） | high |
| AI 模型服务 | `.env.example` 的 `DEEPSEEK_API_KEY`/`DASHSCOPE_API_KEY`/`ZHIPU_API_KEY`/`OPENAI_API_KEY`/`LLM_BACKEND`；`app/engine.py` | high |
| 收件方邮件服务器 | `email_verifier.py`（SMTP 探测，不发送 DATA）；`config.py:35-45` | high |
| Apple 日历 | `app.py:13470`（旧行号）`/api/calendar/ical/<token>.ics` 返回 `text/calendar`；`ical_gen.py` | high |
| Web 工作台前端 | `app.py:126-133`（`static_url_path=''`）；`app/static/app.js` 8,480 行 | high |
| Flask 应用进程 | 158 条路由（实测 `grep -cE '^@app\.(route\|get\|post\|put\|delete)'`）；**`register_blueprint` + `add_url_rule` 计数 = 0**；`serve.py:26-34` 以 `importlib` 按 `trade_os_web` 加载 `app.py` 以避开 `app/` 包名冲突 | high |
| PostgreSQL（单一事实源） | `db.py` 的 `postgres_mode()`、`formal_runtime()`、`require_formal_postgres_runtime()` 门控；`postgres_schema_contract.py:18-65`（实测计数：**63 个必需关系、38 个必需视图**） | high（代码契约） |
| 附件与回滚材料存储 | `deploy/cloud/workbench.env.example:10` `TRADE_OS_DATA_DIR=/var/lib/trade-os`；`backup-workbench.sh:17` | high |
| 沟通采集浏览器扩展 | `browser-extension/manifest.json:2-16`；`browser-extension/sidepanel.js:6` `apiBase` 指向 `app.trosa.space` | high |

## 路由面证据（本轮自行核算）

| 事实 | 数值 |
| --- | --- |
| 路由总数 | **158** |
| `/api` 前缀种类 | **31** |
| API 路由 | **154** |
| 非 API 路由 | **4**（`/`、`/favicon.ico`、`/invite/<token>`、`/share/weekly`） |
| 最大前缀 | `customers` 36 ｜ `integrations` 21（Sela 16 + Gmail 5）｜ `gateway` 11 ｜ `reminders` 9 ｜ `inbox` 8 ｜ `agent` 8 |

## L3 组件证据（全部来自真实 import 边）

| 边 | 证据 | 置信度 |
| --- | --- | --- |
| `app.py` → `db` / `ical_gen` / `scheduler` / `app.engine` / `config` / `gmail_sync` / `trosa_domain` | `app.py:42`、`:50`、`:51`、`:52`、`:63`、`:64`、`:74` | high |
| `db` → `postgres_schema_contract` / `config` / `postgres_compat`（延迟） | `db.py:20`、`:331`、`:443`、`:471` | high |
| `trosa_domain` → `db`（仅 `postgres_mode`） | `trosa_domain.py:22` | high |
| `gmail_sync` → `app.engine` / `db` / `trosa_domain` | `gmail_sync.py:26`、`:27`、`:36` | high |
| `email_verifier` → `config` / `db` | `email_verifier.py:14`、`:15` | high |
| `scheduler` → `db` / `email_verifier` / `gmail_sync` / `config` | `scheduler.py:8`、`:35`、`:55`、`:96`、`:113` | high |
| `serve.py` → `db` / `scheduler` | `serve.py:18-19` | high |
| 模块间无循环依赖 | 上述边集的有向图无环 | high |

模块行数（16:28 实测）：`app.py` **15,152** ｜ `app/static/app.js` 8,480 ｜ `db.py` 2,139 ｜ `app/engine.py` 1,550 ｜ `trosa_domain.py` 1,294 ｜ `gmail_sync.py` 1,263 ｜ `postgres_compat.py` 317 ｜ `postgres_schema_contract.py` 310 ｜ `email_verifier.py` 297 ｜ `scheduler.py` 161 ｜ `ical_gen.py` 143 ｜ `serve.py` 62 ｜ `config.py` 45。

## 业务域证据（`trosa_domain.py`，函数行号本轮实测）

| 结论 | 证据 | 置信度 |
| --- | --- | --- |
| 外联投递历史不能成为待办 | `customer_tasks:29` 文档明写 *"Retired outreach scheduler rows are delivery history, not tasks… cannot become a next step"*；且 SQLite 分支 SQL 带 `NOT LIKE 'outreach_%'`（`:56`） | high（规则 + SQL 强制） |
| Today 与客户工作区共用同一排序 | `customer_tasks:29` 文档："the one ordering used by Today and Customer"；排序键 未完成 → 到期日 → 手工顺序 → id | high |
| 同到期日合并任务 | `merge_open_task:231`；完成 `complete_task:331`、`complete_open_follow_up_tasks:356` | high |
| 客户事实有单一共享投影 | `customer_facts:1187` | high |
| 客户软删除 | `set_customer_deleted:685` | high |
| 等级词汇收窄且数据库强制 | `db.py:992` `CUSTOMER_LEVEL_VALUES = ('A','A+','A-','B','B+','B-','C','C+','C-','D','D+','D-')`；`db.py:1746` 拼进 SQL 允许值列表 | high |
| 客户阶段/等级/判词写入函数存在 | `set_customer_stage:897`、`set_customer_level:921`、`set_customer_judgment:461`、`update_customer_priority:706` | high（函数存在）／**阶段与判词取值 unknown** |
| 外部系统单一适配入口 | `record_external_interaction:151`（Gmail 与 Sela 的适配边界） | high |
| Inbox 独立生命周期 | `create_inbox_item:550` → `assign_inbox_customer:525` → `set_inbox_status:508` / `resolve_inbox_item:482` | high |
| 联系人读写 | `customer_contacts:64`、`create_contact:382` | high |

## 数据拓扑证据

| 事实 | 证据 | 置信度 |
| --- | --- | --- |
| 6 个 schema 与 43 张基表 | `migrations/0001_unified_trade_os.sql`；`postgres_schema_contract.py:14-16` | high |
| 兼容层与写边界 | `migrations/0003`–`0006` | high |
| **兼容写边界触发器挂在两个关系上** | `0023:89-96`：`zz_customers_ref_payload_write` 为 `INSTEAD OF INSERT OR UPDATE OR DELETE`，先 `DROP` 再 `CREATE` 于 `trosa.customers`，同样动作再作用于 `trade_os_compat.customers` | high |
| 兼容写入防覆盖规范状态 | `0026`：改写为读 `to_jsonb(NEW)`（防止兼容视图缺少字段时抹掉规范状态） | high |
| 规范审计写入路径已在代码中确认 | `app.py` `_record_operation_log()`：*"Write one operation audit fact and its optional old-client projection."* | high |
| 0028 的规范表、索引、列均在契约中 | `postgres_schema_contract.py:34`（表）、`:125-126`（索引及列元组）、`:129-131`（`target_reference` 列） | high |
| 两份迁移清单一致且都含 0028 | `db.py:90`；`tools/unified_postgres_migration.py:73` | high |
| 冻结面退役 | `migrations/0019`：删除 AI 研究/理解/推荐/网站监控的兼容视图，**基表保留** | high |
| 网站监控不在运行路径 | `app.py` 中 `web_monitor` / `website_monitor` / `监控` 均无匹配 | high |
| 迁移账本 | `db.py:119-125` 与 `tools/unified_postgres_migration.py:213-219` 创建 `audit.schema_migrations(name, sha256, applied_at)`；已应用迁移内容变化即拒绝 | high |
| `sela.*` 表不在契约中 | `postgres_schema_contract.py` 未声明 `sela` schema 业务表；仅 `tools/unified_postgres_import.py` 引用 | medium |
| 契约校验入口 | `postgres_schema_contract.py:210-310` `schema_status()`，由 `db.py` 在启动前调用；正式启动再由 `serve.py` 在迁移前强制 PG | high |

## 运行拓扑证据

| 事实 | 证据 | 置信度 |
| --- | --- | --- |
| 服务单元与启动命令 | `deploy/trade-os.service:9-11`（`User=tradeos`、`WorkingDirectory=/opt/trade-os/current`、`ExecStart=/opt/trade-os/venv/bin/python /opt/trade-os/current/serve.py`） | high |
| 生产强制 `CRM_ENV=production` 与 PostgreSQL | `serve.py` 调用 `require_formal_postgres_runtime()`；`db.init_all_dbs()` 也有同一正式 guard | high |
| Waitress 绑定与并发 | `serve.py:51-54`：`CRM_PORT` 默认 8080、`CRM_BIND_HOST` 默认 `127.0.0.1`、`threads=CRM_THREADS` 默认 8 | high |
| 调度器与 Web 同进程 | `serve.py:47` `start_scheduler()` | high |
| Tunnel 单元 | `deploy/cloudflared.service:11`，`Requires=trade-os.service` | high |
| 数据库凭据注入 | 文档记录为 systemd drop-in `/etc/systemd/system/trade-os.service.d/postgres.conf`（`TROSA_MAINTENANCE.md:26`），**不在仓库中** | medium |
| 发布以 commit SHA 为锚 | `deploy/cloud/publish-workbench.sh:65-70` | high |
| 远端发布步骤 | `deploy/cloud/publish-remote.sh`：`flock` → 下载 `codeload` 压缩包 → 解包到 `/opt/trade-os/releases/$RELEASE_ID` → `pip install` → `py_compile` → 原子符号链接切换 → `systemctl restart` → 健康探测，失败回滚并保留 5 个版本 | high |
| 发布前本地门禁 | `deploy/cloud/auto-publish.sh`：仅 `main`、拒绝 `git add .`、拒绝未暂存已跟踪改动、提交黑名单、DB 敏感文件的破坏性 SQL 扫描、临时 `CRM_DB_PATH` 单测、`py_compile`、`node --check`、扩展 `npm test` | high |
| 发布后公网健康检查失败**不自动回滚** | `auto-publish.sh:325-337` | high |
| PostgreSQL 备份职责分离 | `db.py:579-581`；`app.py` 恢复接口在 PG 模式返回 409 `managed_externally` | high |
| 生产备份与还原校验 | `deploy/postgres-production/backup.sh`、`restore-check.sh` | high |
| 本地隔离 rehearsal | `tools/postgres_rehearsal.py`：`127.0.0.1:55432`、库 `trosa_rehearsal`、拒绝非回环主机 | high |
| 正式运行契约已在代码中强制 | `db.py` 的 `trosa-postgresql-v1`、`serve.py` 启动 guard、`app.py` `/api/network/ping`；ECS release、公网 ping 与服务重启记录均已核实 | **high（本地代码 + ECS live）** |

## 假设（human-assumption，显式标注）

1. **「兼容层」处于收尾阶段**：0023/0026 的写边界设计目标是让 SQLite 形状的旧写入面继续可用，同时以 `trosa.customer_details` / `trosa.customer_states` 为规范。模型按此语义绘制，但未找到「何时移除兼容层」的书面计划。
2. **组件划分的粒度**：`app.py` 是单个模块，代码中不存在模块级边界。C4 的 L3 组件视图以**文件/模块**为单位（有 import 证据）；`app.py` 内部的「功能分组」是**分析性划分**，不是代码强制边界。
3. **`trosa_domain` 的写入落点**：确认存在 `create_customer`/`update_customer` 等业务写入函数，但未逐列核实其落库目标是基表还是可写视图。

## 未决校验任务

| # | 问题 | 为什么重要 | 建议校验方式 |
| --- | --- | --- | --- |
| 1 | **ECS 是否已运行当前 release** | **已确认**：release `auto-20260912082853-0a25827`，远端 `app=active`、`tunnel=active`、`health=ok`，公网 ping 四字段通过 | 后续发布重复 `deploy/cloud/status-workbench.sh` 和公网 ping 验收 |
| 2 | **生产库是否已应用 `0029`** | **已确认**：受控 `verify_schema` 返回 `ok=true`，0001–0029 ledger/hash、schema contract 和 orphan reference 检查均通过 | 后续新增迁移沿用受控 `verify_schema` |
| 3 | Cloudflare Access 是否启用 | `TROSA_MAINTENANCE.md:16` 画出 Access/Tunnel，但仓库脚本未强制任何 Access 策略；若未启用，应用层登录就是唯一门禁 | 检查 Cloudflare Zero Trust 控制台（仓库外） |
| 4 | `sela.*` schema 是否仍有历史 importer 之外的写入 | 当前正式 Sela 运行不依赖该 schema，避免把历史导入面误当成运行内核 | 对运行库检查最近写入来源 |
| 5 | 客户"阶段"与"判词"的全量取值语义及运行角色降权状态 | 关系隔离与权限仍需生产级验证 | 读 `trosa_domain.py`/迁移 SQL，并执行只读权限矩阵演练 |
| 6 | 业务表列级外键与生命周期取值 | 本模型只到表/视图粒度 | 读 `migrations/0020`、`0022` |

## 复现方式

```bash
# 规模、路由面与指纹
wc -l app.py db.py trosa_domain.py gmail_sync.py app/engine.py app/static/app.js
grep -cE '^@app\.(route|get|post|put|delete)' app.py
grep -c 'register_blueprint\|add_url_rule' app.py
grep -oE "^@app\.(route|get|post|put|delete)\('/api/[a-z0-9-]+" app.py | sed -E "s/.*'\/api\///" | sort | uniq -c | sort -rn
shasum -a 256 app.py db.py trosa_domain.py postgres_schema_contract.py serve.py

# 模块导入边
for f in app.py db.py trosa_domain.py gmail_sync.py email_verifier.py scheduler.py serve.py; do
  echo "--- $f ---"; grep -nE '^\s*(import|from)\s+(db|postgres_compat|postgres_schema_contract|trosa_domain|gmail_sync|email_verifier|scheduler|ical_gen|config|app\.engine)\b' "$f"
done

# 迁移清单一致性（两份应一致并都含 0028）
sed -n '60,92p' db.py
sed -n '45,76p' tools/unified_postgres_migration.py

# 契约计数（应为 63 / 38）
python3 -c "import postgres_schema_contract as c; print(len(c.REQUIRED_TABLES), len(c.REQUIRED_VIEWS))"

# 渲染图（本机无原生 dot，已改用 WASM 构建；见下）
dot -Tsvg docs/architecture/trosa-data-topology.dot -o docs/architecture/trosa-data-topology.svg
dot -Tsvg docs/architecture/trosa-runtime-topology.dot -o docs/architecture/trosa-runtime-topology.svg
```

### 实际使用的渲染路径（2026-09-12）

本机 `brew install graphviz` 失败：Homebrew 判定需从源码构建 `librsvg`/`rust`，并在下载 `ghcr.io` 上的 `giflib` bottle manifest 时中断（`curl: (35) LibreSSL SSL_connect: SSL_ERROR_SYSCALL`）。因此 SVG 由 **Graphviz 16.0.0 的 WASM 构建**（`@viz-js/viz@3.30.0`，与其将安装的原生版本号一致）渲染：

```bash
mkdir -p /tmp/svg-render && cd /tmp/svg-render && npm i @viz-js/viz
node -e '
const fs=require("node:fs");
import("@viz-js/viz").then(async ({instance})=>{
  const viz=await instance();
  for (const f of process.argv.slice(1)) {
    fs.writeFileSync(f.replace(/[.]dot$/,".svg"), viz.renderString(fs.readFileSync(f,"utf8"),{format:"svg",engine:"dot"}));
  }
});' /abs/path/trosa-data-topology.dot /abs/path/trosa-runtime-topology.dot
```

渲染后已核对：两个 SVG 均为合法 XML，节点/边数与源文件一致（数据拓扑 43 节点 / 30 边 / 7 簇；运行拓扑 19 / 20 / 3），中文标签完整未乱码。数据拓扑由 TB 改为 `rankdir=LR`：TB 下画布为 4211×621pt（6.8:1，文字不可读），LR 下为 1517×2185pt。

**渲染身份说明**：WASM 的 Graphviz 版本与 brew 将要安装的版本一致（同为 16.0.0），但布局引擎来源不同；若日后用原生 `dot` 重新渲染，坐标可能有细微差异，图的结构与读数不变。
