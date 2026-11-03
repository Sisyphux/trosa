# 维护者读取全员数据的只读通道

## 为什么有这条通道
- Agent Gateway 令牌只对一个成员生效，开发/维护 Agent 看不到其他成员的数据，就无法验证“跨成员”问题（例如排除清单是否漏掉别的成员已联系的公司）。
- 发布配置 `workbench.env` 同时带有发布能力，必须继续对开发/审查会话关闭。
- 因此读数据用**独立的只读数据库角色** `trosa_maint_ro`，凭据放在 `~/.config/trosa-maint/readonly.json`（仓库外、权限 600），与发布配置完全分开。

## 它能做什么 / 不能做什么
| 能 | 不能 |
|---|---|
| 读所有成员的业务数据（`trosa`、`core`、`audit`、`sela` 等 schema） | 任何写入、DDL、`COPY`、调用写函数 |
| `identity` 里只读成员、归属、组织 | 读邀请、设置等 `identity` 其他表 |
| 用 `--as-user <成员>` 以该成员视角读投影视图 | 发布、读取 `workbench.env`、领取/撤销令牌 |

三道护栏：数据库角色只有 SELECT 且 `default_transaction_read_only=on`；连接强制只读事务与 20 秒超时；工具本身只放行单条 SELECT/WITH/EXPLAIN。每次调用追加到 `~/.config/trosa-maint/audit.log`。

## 一次性开通（发布角色或管理员在正式主机 `trosa-new` 上做）
1. 在正式主机 `trosa-new` 的 `deploy/postgres-production/`（正式部署目录是 `/opt/trade-os-postgres`）运行
   `./maint-readonly-role.sh`（幂等）。脚本默认连接正式库 `tradeos`，管理角色 `tradeos_app`。
   旧的 `trosa-ecs` 只保留回退快照，已停用，不要在上面开通。
2. 把生成的密码（`secrets/maint_readonly_password`）写入维护者 Mac 的 `~/.config/trosa-maint/readonly.json`，权限 600：
   ```json
   {"dsn": "postgresql://trosa_maint_ro:<密码>@127.0.0.1:15432/tradeos",
    "ssh_host": "trosa-new", "remote_port": 5432, "local_port": 15432}
   ```
   有 `ssh_host` 时工具会自己开关隧道；PostgreSQL 不对公网开放。
3. 验证：`python3 tools/trosa_maint_read.py check` 退出码 0，输出里
   `db_name=tradeos`、`db_role=trosa_maint_ro`、`read_only=on`，且 `data_lag_hours` 小于 48。

## 日常用法
```bash
python3 tools/trosa_maint_read.py whoami                                 # 角色 / 库名 / 数据水位
python3 tools/trosa_maint_read.py check                                  # 严格自检，过期返回 3
python3 tools/trosa_maint_read.py users
python3 tools/trosa_maint_read.py customers --query bold.net.co          # 遍历全部成员
python3 tools/trosa_maint_read.py sql "SELECT ..." --as-user kelley
```

## 数据新鲜度与旧快照
- `whoami` / `check` 会输出 `data_watermark`（业务表最后更新时间）和 `data_lag_hours`；
  默认滞后超过 48 小时即视为可疑。`check` 在以下情况返回 3：数据过期、`ssh_host` 是已退役的
  `trosa-ecs`、或连到的数据库不是 `tradeos`。
- 读到几天前的数据，通常不是数据库坏了，而是隧道还连在旧主机上：本机若已有一条指向
  `trosa-ecs` 的 `ssh -N -L 15432:...` 进程，工具会复用这个端口。现在工具会检查端口占用进程，
  发现目标主机与配置不一致时直接报错；按提示结束旧隧道，或改 `local_port`。
- 排查命令：`lsof -nP -iTCP:15432 -sTCP:LISTEN`、`ps -o command= -p <pid>`。

## 边界
- 只读，仅用于定位问题与验证修复；结论写进变更记录时不要贴客户私密内容原文。
- 业务写入仍走 Agent Gateway / 应用 API，不经过这里。
- 不要把凭据文件、密码或 DSN 贴进对话、提交或 `workbench.env`。
