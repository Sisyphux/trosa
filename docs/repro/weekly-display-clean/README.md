# 周报沟通展示清理

## 业务结果与验收

此前，本周工作和点客户名打开的只读详情，会把客户背调边界与 ticket 编号一起展示。现在正文及下一步隐藏 `【客户背调】`、带 YYYY-MM-DD 日期的客户背调开头、`【/客户背调】` 和 `[ticket:数字]`，保留边界内正文、普通日期、金额、数量、订单标记及高亮。

影响范围为周报与对应只读详情的本周、更早沟通。原始 API 数据、周报缓存、全局富文本渲染和其他沟通视图保持原样。此次仅为显示处理，不执行数据清理。

简单验收方法为打开带有合成记录的本周工作，确认只看到正文，再点 Synthetic Acrylic，检查本周和更早两组记录。`105529 元`、`2026-11-12`、`3mm 200张`、`[订单:105529]`、`2026-11-20`、`12 件` 和绿色高亮都应保留。

匹配边界采用保守的精确格式。`[ticket:abc]`、`【客户背调 其他备注】` 和其他方括号内容保留。若正文恰好包含上述精确内部标记格式，也会被隐藏，原文仍保留。带空格或其他未知变体的标记不会被清理。单元测试覆盖这些边界。

## 真实浏览器红绿证据

先运行原有脚本，产品代码修改前取得退出 20，18/29 项通过，11 项内部标记隐藏检查失败。新增边界单元测试也先失败，再实现周报专用显示处理。修复后同一浏览器脚本退出 0，29/29 项通过，无页面错误。Chromium 版本见各 report.json。

脚本通过 Playwright 拦截周报与详情接口并返回合成数据。仅连接 loopback 隔离 SQLite 服务。截图和报告分别在 before/、after/，已检查桌面周报与手机详情截图。

## 精确命令

在当前 worktree 根目录执行。服务在第一条命令中持续运行，浏览器命令使用另一个终端。

```sh
TMPDIR=/Users/luoxin/.hermes/cache/scratch PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tools/repro_dev_server.py --app-root . --port 18196 --db-dir /Users/luoxin/.hermes/cache/scratch/trosa-weekly-display-clean-db

TMPDIR=/Users/luoxin/.hermes/cache/scratch node tools/repro_weekly_display_clean.cjs --url http://127.0.0.1:18196 --label before --out docs/repro/weekly-display-clean/before

TMPDIR=/Users/luoxin/.hermes/cache/scratch node tools/repro_weekly_display_clean.cjs --url http://127.0.0.1:18196 --label after --out docs/repro/weekly-display-clean/after

TMPDIR=/Users/luoxin/.hermes/cache/scratch PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_weekly*.py' -v
TMPDIR=/Users/luoxin/.hermes/cache/scratch PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*communication*.py' -v
node --check app/static/app.js
node --check tools/repro_weekly_display_clean.cjs
git diff --check
```

在 browser-extension/ 中执行的命令如下。

```sh
TMPDIR=/Users/luoxin/.hermes/cache/scratch npm test
```

周报测试 10 项、沟通文本测试 35 项、扩展的四组测试及语法和 diff 检查全部通过。浏览器覆盖周报 1440/1280/960/768/390 宽度，详情 1440/768/390 宽度及原始周报缓存、全局渲染保留检查。尚未验证真实旧 Win10、iPad 设备、200% 缩放与完整键盘流程，也未运行全库测试。

本轮按用户约束仅写入本 worktree 与指定 scratch。未提交、推送或发布，未访问正式数据。之前的沙箱启动失败已由本次真实 Chromium 红绿证据取代。

## 2026-11-16 客户简介补修（本轮）

此前未提交的修复漏掉了 `customer_profile`、`customer_profile_zh`、详情的 `profile` / `profile_zh` 以及延迟翻译更新。现在这些简介及其 `title` 原文提示都经过周报显示清理：隐藏背调边界及边界内研究日期、数字 ticket 标记，将已有 mark/br 富文本转为可读纯文本。普通业务日期、订单号、金额与数量保留。API、缓存对象、数据库及其他页面的原文不修改。只匹配已知标记格式，未知变体仍保留；正文中恰好出现同样格式也会隐藏。

验收：在本周工作打开有背调标记的客户，检查列表简介，点击客户名检查详情简介，等待翻译出现后再检查。简介和鼠标悬停原文均不应出现 `【客户背调 2026-11-10】`、闭合标记、数字 ticket 或原始 mark/br 标签；应保留 `2026-12-20` 交货日期和客户业务描述。

先新增真实生产渲染函数测试，再修改产品代码。修复前 `test_weekly_page_visual.py` 共 7 项，6 项通过、1 项失败，失败为 `list: internal metadata leaked`。修复后 7 项全部通过。新测试覆盖列表和详情的原文/译文、详情延迟翻译后的文字与 title，以及原始对象不变。

本轮验证（沙箱外重新执行）：
- `test_weekly*.py`：11 项全部通过，包括先前因沙箱禁止 socket bind 而出错的 gateway 测试。
- `test_*communication*.py`：35 项通过。
- `browser-extension/npm test`：四组全部通过。
- 两个 JS 文件语法检查及 `git diff --check` 通过。
- 未运行全库测试。

初次在受限沙箱中启动本地服务及 Chromium 失败；随后在沙箱外使用隔离 loopback 服务和合成 API 数据重新运行，Chromium 153.0.8010.12 的 49/49 项检查通过、无页面错误。检查覆盖列表和详情的简介、title、延迟翻译、原始缓存及桌面/移动宽度；截图和报告位于 `profile-after/`。已查看 1440px 列表和详情截图，简介可读且未显示标记或背调日期。测试使用合成数据，并非生产数据。

未提交、部署、撤销任何已有改动，未访问正式数据。
