# 演练进程卫生（rehearsal hygiene）

## 为什么需要它

发布门禁（`deploy/cloud/release-test.sh`）每次都会启动隔离的演练资源：

- 一个 `serve_rehearsal.py` Web 服务（浏览器验收用，绑定随机 loopback 端口）；
- 一个 loopback PostgreSQL 演练集群；
- `$TMPDIR` 下一批临时目录（回归数据目录、日志、socket 目录、release 临时树）。

门禁被中断、被 `SIGKILL`，或子进程活得比启动它的 shell 更久时，这些资源不会
被回收。并行开发一整天后就会积累出几十个孤儿 Web 服务，每个都占一个端口并消耗
CPU，让之后每次门禁都像“卡住”。

`tools/rehearsal_hygiene.py` 就是监控并清理这批遗留物的唯一程序。它刻意保守：

- 只有**父进程已经消失**（被 PID 1 收养）的演练 Web 进程才会被回收；父进程仍在
  的进程属于正在运行的门禁，只会被报告为 `active`，绝不会被碰。
- PostgreSQL 演练集群只在显式 `--include-postgres` 且没有门禁引用其目录/端口时
  才停止。
- 临时文件只有在没有任何活进程引用（并检查进程环境变量，如 `CRM_DB_PATH`）且
  超过 `--min-temp-age` 时才算过期。
- `clean` 默认是 dry run，只有 `--apply` 才真正执行。

## 用法

```bash
# 只读报告（默认命令）
python3 tools/rehearsal_hygiene.py scan
python3 tools/rehearsal_hygiene.py scan --json          # 机器可读
python3 tools/rehearsal_hygiene.py scan --check          # 有孤儿 Web 进程时退出码 2

# 清理：先看 dry run，再加 --apply
python3 tools/rehearsal_hygiene.py clean
python3 tools/rehearsal_hygiene.py clean --apply
python3 tools/rehearsal_hygiene.py clean --apply --orphans-only          # 只清孤儿 Web 进程
python3 tools/rehearsal_hygiene.py clean --apply --include-postgres      # 附带停止空闲 PG
python3 tools/rehearsal_hygiene.py clean --apply --include-worktrees     # 附带删除过期临时 worktree

# 持续监控（清理机器长期低负载；Ctrl-C 停止）
python3 tools/rehearsal_hygiene.py watch --interval 60 --apply --orphans-only
```

## 与门禁的协作

`tools/browser_acceptance.sh` 在启动自己的服务之前会先执行
`clean --apply --orphans-only`，自动回收上一次中断留下的孤儿服务。并发运行的其它
门禁服务仍持有活父进程，因此不会被误杀。这样堆积只清理不增长。

## 安全边界

- 不读取、不修改任何业务数据；只操作本机进程与 `$TMPDIR` 下的门禁临时物。
- 不触碰 ECS、正式数据或 `data/`；这与生产运行契约无关，纯粹是本机开发卫生。
- `--apply` 前建议先跑一次 `scan` / `clean`（dry run）核对目标。
