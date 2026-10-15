# 维护者读取全员数据的只读通道

## 为什么有这条通道
- Agent Gateway 令牌只对一个成员生效，开发/维护 Agent 看不到其他成员的数据，就无法验证“跨成员”问题（例如排除清单是否漏掉别的成员已联系的公司）。
- 发布配置 `workbench.env` 同时带有发布 ECS 的能力，必须继续对开发/审查会话关闭。
- 因此读数据用**独立的只读数据库角色** `trosa_maint_ro`，凭据放在 `~/.config/trosa-maint/readonly.json`（仓库外、权限 600），与发布配置完全分开。

## 它能做什么 / 不能做什么
| 能 | 不能 |
|---|---|
| 读所有成员的业务数据（`trosa`、`core`、`audit`、`sela` 等 schema） | 任何写入、DDL、`COPY`、调用写函数 |
| `identity` 里只读成员、归属、组织 | 读邀请、设置等 `identity` 其他表 |
| 用 `--as-user <成员>` 以该成员视角读投影视图 | 发布、读取 `workbench.env`、领取/撤销令牌 |

三道护栏：数据库角色只有 SELECT 且 `default_transaction_read_only=on`；连接强制只读事务与 20 秒超时；工具本身只放行单条 SELECT/WITH/EXPLAIN。每次调用追加到 `~/.config/trosa-maint/audit.log`。

## 一次性开通（发布角色或管理员在 ECS 上做）
1. 在 ECS 的 `deploy/postgres-production/` 运行 `./maint-readonly-role.sh`（幂等）。
2. 把生成的密码（`secrets/maint_readonly_password`）写入维护者 Mac 的 `~/.config/trosa-maint/readonly.json`，权限 600：
   ```json
   {"dsn": "postgresql://trosa_maint_ro:<密码>@127.0.0.1:15432/trade_os",
    "ssh_host": "<ssh 别名>", "remote_port": 5432, "local_port": 15432}
   ```
   有 `ssh_host` 时工具会自己开关隧道；PostgreSQL 不对公网开放。
3. 验证：`python3 tools/trosa_maint_read.py whoami` 应显示 `db_role=trosa_maint_ro`、`read_only=on`。

## 日常用法
```bash
python3 tools/trosa_maint_read.py users
python3 tools/trosa_maint_read.py customers --query bold.net.co          # 遍历全部成员
python3 tools/trosa_maint_read.py sql "SELECT ..." --as-user kelley
```

## 边界
- 只读，仅用于定位问题与验证修复；结论写进变更记录时不要贴客户私密内容原文。
- 业务写入仍走 Agent Gateway / 应用 API，不经过这里。
- 不要把凭据文件、密码或 DSN 贴进对话、提交或 `workbench.env`。
