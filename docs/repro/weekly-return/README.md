# 周报页回不去操作台

- 日期：2026-10-09
- 修复前基线：`0e486d6d`（用 `git archive` 导出到临时目录运行，不改动工作区）
- 浏览器：Chromium 153.0.8010.12（Playwright 1.63.0，由 `browser-extension/package-lock.json` 锁定）
- 复现脚本：[`tools/repro_weekly_return.cjs`](../../../tools/repro_weekly_return.cjs)；运行方法见 [`docs/REAL_BROWSER_REPRO.md`](../../REAL_BROWSER_REPRO.md)

## 结论

| 运行 | 结果 | 退出码 |
| --- | --- | --- |
| `before/`（修复前） | 4 / 8 项通过，缺陷复现 | 20 |
| `after/`（修复后） | 8 / 8 项通过 | 0 |

## 现象

用办公网内部访问者的身份打开应用，会直接进入“本周工作”。在这一页打开“导航与搜索”，此时（约 945px 宽，截图中 1890px@2x 的那一档）：

- “今天 / Inbox / 客户”三行只剩框线，没有名字，也没有序号。Inbox 上的数字（如 13）仍然显示。
- 底部工具行的文字正常，所以看起来像“只有几个不知道干什么的空行”。
- 点这些空行，会弹出“登录已过期，请重新登录”，并跳回账号选择页。

见 [`before/02-index-weekly-945.png`](before/02-index-weekly-945.png)，与用户截图一致。

## 根因

两处都是 CSS 层叠问题，JavaScript 的状态是对的。

1. **名字和序号被隐藏**：[`app/static/visual-v3.css`](../../../app/static/visual-v3.css) 有一条旧侧栏规则 `.nav-item > span:not(.nav-icon):not(.nav-count) { display: none !important }`。它在 621–1180px 宽度下生效（≤620px 与 ≥1181px 有 `revert` / `block` 覆盖），于是导航行里的 `.nav-name` 和 `.nav-index` 全被隐藏。“导航与搜索”是全屏层，没有侧栏，这条规则不该作用到它。
2. **周报模式仍显示个人入口**：[`app/static/visual-v5.css`](../../../app/static/visual-v5.css) 的 `#trosa .room-index .nav-personal.nav-personal { display: block !important }` 覆盖了 [`enterOverview()`](../../../app/static/app.js) 设置的内联 `display: none`。于是未登录的局域网访问者在周报页也能看到“今天 / Inbox / 客户”。点击后请求 `/api/stats` 等接口返回 401，触发 `showLogin()` 的“登录已过期”提示。

## 修复

- [`app/static/visual-v5.css`](../../../app/static/visual-v5.css)：去掉 `.nav-personal` 的 `!important`，让内联隐藏生效；为房间导航的 `.nav-index`、`.nav-name` 写更具体的选择器并强制 `display: block`，压过旧规则；`.room-index-more button[hidden]` 强制隐藏。
- [`app/static/index.html`](../../../app/static/index.html)：完整日历、沟通记录、未建立真实沟通、操作日志、设置这五个需要登录的工具按钮加上 `room-personal-tool`。
- [`app/static/app.js`](../../../app/static/app.js)：`enterOverview()` 隐藏这五个工具按钮，`showApp()` 再把它们显示出来。

没有改接口、数据或迁移。

修复后，周报模式的导航只剩“本周工作”和“切换账号”。回到个人操作台的方式是点“切换账号”，再选自己的名字（开发库没有访问码；生产环境按原流程输入访问码）。

## 修复前的“回不去”到底是什么

脚本里的 `way-back-lands-on-today` 在修复前就通过了：经“切换账号”选择成员，仍然能回到“今天”。所以用户感到回不去，主要来自两点：

- 导航行没有名字，看不出哪一行是“今天”。
- 误点个人入口时弹出的“登录已过期”会让人以为账号出了问题，而实际从未登录。

这两点都已修复。

## 检查项

| 检查 | 修复前 | 修复后 |
| --- | --- | --- |
| 只看周报时，不显示需要登录的个人入口 | 失败：今天、Inbox、客户仍显示 | 通过 |
| 只看周报时，可见的导航行都有名字 | 失败：可见的行都没有名字 | 通过 |
| 只看周报时点个人入口，不弹出“登录已过期” | 失败：点“今天”后返回 401 | 通过（入口未提供） |
| 经“切换账号”选择成员后回到今天 | 通过 | 通过 |
| 返回过程中没有 401 | 通过 | 通过 |
| 登录后，945px 宽时个人导航有名字 | 失败：无名字 | 通过 |
| 登录后，1440px 宽时个人导航有名字 | 通过 | 通过 |
| 登录后，375px 宽时个人导航有名字 | 通过 | 通过 |

## 证据文件

`before/` 和 `after/` 各有一份 `report.json`（逐项结果、401 路径、页面错误、每行导航的测量数据）。截图按步骤编号：

| 文件 | 内容 |
| --- | --- |
| `01-weekly-board-945.png` | 启动后直接进入周报 |
| `02-index-weekly-945.png` | 周报页打开导航：修复前为空行，修复后只剩“本周工作” |
| `03-account-chooser-945.png` | 切换账号后的账号选择页 |
| `04-workbench-after-return-945.png` | 选择成员后回到“今天” |
| `05-index-signed-in-945.png` | 登录后打开导航（945px） |
| `05-index-signed-in-375.png` | 登录后打开导航（375px，手机宽度） |

证据里隐藏了登录页上列出的办公网访问地址（只是截图时隐藏，不影响页面布局），这些地址不属于缺陷内容，也不应进入仓库。

## 未处理的已知问题

- 控制台有 `InvalidStateError: Transition was aborted because of invalid state`（视图过渡被快速切换打断）。修复前后都有，不影响本次检查结果，本次不改。
