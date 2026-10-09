# 真实浏览器重现界面缺陷

适用于：页面看不见、点不动、点了跳到别处、只在某个宽度出现的问题。规则只有一条：**修复前先在真实浏览器里重现，修复后用同一个脚本验证，并把证据存进仓库。** jsdom 和读代码只能说明逻辑，不能证明 CSS 层叠和布局，这类结论不足以说“已修复”。

重现示例与证据：[`docs/repro/weekly-return/README.md`](repro/weekly-return/README.md)（周报页回不去操作台）。

## 1. 准备（每个 worktree 做一次）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd browser-extension && npm ci
```

`npm ci` 会安装锁定的 Playwright（1.63.0）。如果启动浏览器时报缺少可执行文件，在 `browser-extension/` 运行 `npx playwright install chromium`，版本仍由锁文件决定。

## 2. 启动隔离的本地服务

用 `tools/repro_dev_server.py` 启动。它只监听 `127.0.0.1`，固定 `CRM_ENV=development` 和 SQLite 后端，清掉 `TRADE_OS_DATABASE_URL`（即使 shell 里残留了 PostgreSQL 地址也不会连过去），并拒绝把仓库的 `data/` 当作数据目录。

```bash
.venv/bin/python tools/repro_dev_server.py --app-root . --port 18190 --db-dir /path/to/scratch/db-after
```

- `--db-dir` 必须是临时目录，例如 AI 会话的 scratchpad，或 `mktemp -d` 得到的目录。
- 修复前对比时，用 `git archive HEAD` 导出一份干净副本到临时目录，把 `--app-root` 指向它。不要切换分支，也不要用 `git stash`。
- 默认把回环地址视为办公网内部访问者（`CRM_INTERNAL_VIEWER_CIDRS`），这样登录页才会出现“本周工作”入口，和办公网的真实情况一致。
- 开发库没有 PIN：在账号选择页点名字即可进入，账号见 `db.py` 的 `USERS`（hamid、amy、kelley）。

## 3. 在内置浏览器里重现（用户可以看到）

内置浏览器不能打开 Claude 自己用 shell 启动的 localhost 服务，所以要通过 `preview_start` 启动。在 `.claude/launch.json` 里为每个服务写一项（路径是本机的，不用提交）：

```json
{
  "version": "0.0.1",
  "configurations": [
    {
      "name": "trosa-after",
      "runtimeExecutable": "<仓库绝对路径>/.venv/bin/python",
      "runtimeArgs": [
        "<仓库绝对路径>/tools/repro_dev_server.py",
        "--app-root", "<仓库绝对路径>",
        "--port", "18190",
        "--db-dir", "<临时目录>/db-after"
      ],
      "port": 18190,
      "url": "http://127.0.0.1:18190"
    }
  ]
}
```

操作要点：

- 启动后用 `read_page` / `get_page_text` 读取页面结构；需要确认样式时用 `javascript_tool` 读 `getComputedStyle`（例如导航行的 `display`）。
- 宽度问题用 `resize_window` 设定视口，例如 945px（截图里 1890px@2x 的那一档）。测完用 preset `desktop` 复原。
- 切换页面有视图过渡动画，截图前要等动画结束，否则可能拍到上一页的残影。
- 每一步都记下点了什么、看到什么，并把关键截图存下来。

## 4. 用脚本重现并保存证据

每个重现场景都有一份脚本，放在 `tools/repro_<场景>.cjs`，检查项都写明了期望结果。运行方式：

```bash
node tools/repro_weekly_return.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/weekly-return/after
```

- 脚本在 `browser-extension` 锁定的 Chromium 中运行，不用桌面浏览器。
- 证据写到 `--out` 指定的目录：`report.json`（每项检查的结果、401 路径、页面错误）和若干截图。
- 退出码：`0` 全部通过；`20` 有检查失败，即缺陷存在；`21` 基础设施故障（服务没起来、浏览器不可用）。

证据约定：`docs/repro/<场景>/before/` 存修复前的结果，`docs/repro/<场景>/after/` 存修复后的结果，`docs/repro/<场景>/README.md` 写结论、根因和修复点。

## 5. 新增一个重现场景

1. 先用内置浏览器手动走一遍，记下准确的路径、宽度和看到的现象。
2. 复制 `tools/repro_weekly_return.cjs`，改写场景步骤和检查项。每一项检查都写“期望什么”，失败时能说明缺陷。
3. 对修复前的代码运行脚本，**必须**得到退出码 `20`；否则脚本没有重现问题，不能用来证明修复。
4. 修复后对同一服务运行，**必须**得到退出码 `0`。
5. 证据和结论存进 `docs/repro/<场景>/`，在 `CHANGELOG.md` 写用户可感知的修复。

## 6. 边界

- 只用本机地址、测试账号和临时数据库。不要连接 ECS、正式备份、仓库里的 `data/` 或 PostgreSQL 正式库。
- 测试账号只存在于本地临时库，不把凭据写进文档或聊天。
- 发布门禁仍使用 `tools/run_browser_acceptance.cjs`（PostgreSQL 演练）。本文的脚本用于重现和验证具体缺陷，不替代发布门禁。
- 证据截图控制在每个场景约 10 张以内，只保留能说明结论的画面。
