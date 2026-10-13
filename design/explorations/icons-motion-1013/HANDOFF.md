# 任务：Trosa「一笔」图标与动效全站落地

## 背景（已定，不要重新讨论）

- 2026-10-13，用户在四个方向里选定 **A「一笔」**：手绘单线，旁边一块错位约 1.4px 的浅灰片；加载是“一张纸被一笔笔写满”。用户的评价是“非常好看，又很精准清楚”，要求在系统里大量使用，让现有页面更生动，并明确授权**可以比方案更激进**。
- 原型与方案都在分支 `agent/icons-motion-1013` 的 `design/explorations/icons-motion-1013/` 下，必读，先在浏览器里看：
  - `index.html`：四个方向的原型，只看 A 节。
  - `plan.html`：全站方案，含现状诊断、三条规矩、五个落点（有可播放示意）、分三期、待拍板事项。页内 `<script>` 里的 `rng / Pen / G / ICONS` 就是图标生成器，34 个图标的画法都在 `ICONS` 里。
  - 本地查看：在仓库根目录运行 `python3 -m http.server <端口> --bind 127.0.0.1`，打开 `/design/explorations/icons-motion-1013/plan.html`。新旧图标对比要求从仓库根起服务。
- 设计基准是 `design/DESIGN_WORLD.md`（2026-10-13 重写的瑞士式：白底墨字、只用 Hanken Grotesk 一种字体、唯一强调色橙红 `#e8431f`、圆角 2px、无阴影、1px 细线）和 `design/UI_QUALITY_BASELINE.md`。

## 用户对待拍板事项的答复：按“更激进”执行

1. **手绘程度**：可以比 plan.html 更明显。把生成器的抖动调到一眼就能看出是手画的（参考 Claude 加载插画的手感），但 16px 下仍须清楚可辨，并用尺寸检查截图证明。
2. **浅灰片**：除了导航、空状态和整页加载，还可以用在页面标题区、焦点区、弹窗标题等“身份”位置。按钮里 ≤16px 的小图标仍然不加，加了会发脏。
3. **直接开始，三期都做。**

“更激进”的边界是硬约束，不能越过：

- 不新增颜色、字体、阴影和圆角。浅灰片用低透明度的墨色，或现有的羽毛灰 `#e4e3df`；橙红只出现在现有位置。
- 必须响应 `prefers-reduced-motion`：动画直接到终态，功能不变。任何核心操作都不能等动画播完。
- 对比度、键盘操作、200% 缩放和触控尺寸都不能退步；图标保留可访问名称。
- 不改数据、接口和写入语义；不破坏客户、联系人、沟通记录、Today、Inbox、Search、备份恢复和 Sela 幂等同步。
- 超出 DESIGN_WORLD 现有规则的做法（例如更多持续动效、每页不止一处点睛），先在同一个提交里修改 `design/DESIGN_WORLD.md`，写明规则和理由，再改代码。

## 现状要点（已调研；开工前用 grep 复核行号）

- **图标**：`app/static/visual-v2.css` 约第 48–82 行的 `.ui-icon` 用 CSS mask 加载 `app/static/icons/phosphor/*.svg`，共 32 个。`app.js` 的 `uiIcon(name)` 负责出图；`applyIconButtons` 配合 `_ICON_ONLY_ACTIONS` / `_INLINE_ICON_ACTIONS`，把按钮文字和 `index.html` 里 12 处内联 SVG 换成 ui-icon。
  - mask 会保留透明度，所以浅灰片（fill-opacity .1）放进 mask 照样成立。
  - mask 做不了描线动画。需要悬停描线的地方（侧栏、导航、今天页快捷动作）改用内联 SVG。
- **等待**：五种写法并存。
  - `loadLoadingHtml`，6 处。
  - `setCustomerSectionLoading` 里的 `.cw-empty`。
  - `setCustomerWorkspaceState` 的进度条。
  - `style.css` 里的三个转圈：`smart-spin`、`connectionStatusSpin`、`action-status-spinner`；后两个对应 `index.html` 第 83–84 行的 `#connectionStatus` 和 `#actionStatus`。
  - 零星的 `skeleton-line`。
- **空状态**：约 30 处纯文字，可用 `grep -n "还没有\|暂无\|没有找到" app/static/app.js` 找到。`.empty-state` 在 js 里 17 处、html 里 10 处。相关函数有 `renderCustomerTimelineEmpty`、`renderCustomerTaskEmpty`、`setTodayWorkspaceEmpty` 等。
- **完成反馈**：`showToast`（`style.css` 的 `toastIn` 位移 20px，超出规范的 8px）、`showToastAction`、`finishActionStatus`，以及各处撤销提示。
- **动效基础**：`_motionReduced`、`isMotionLite()`、`startViewTransition`。时长 token 为 `--dl-dur-fast .16s`、`--dl-dur .28s`、`--dl-dur-slow .55s`，缓动用 `--dl-ease`。
- **样式层**：外观的最后一层是 `app/static/swiss-d.css`，选择器写法为 `html[data-type="swiss"][data-type][data-type] … :is(#trosa,#trosa *)`。新样式放在这里，或放进新文件并在 `index.html` 最后引入。

## 分三期（每期一个独立任务、一个逻辑完整的提交）

**一期 · 底子**

- 把 plan.html 里的生成器固化成脚本（例如 `tools/hand_icons.mjs`，用确定性种子），输出静态 SVG 到 `app/static/icons/hand/`，生成结果一并提交。
- 图标映射改指向新文件，可以改 `visual-v2.css`，也可以在 `swiss-d.css` 里覆盖；新增 `done`、`today` 等。线宽按 16 / 20 / 24px 分档。
- 侧栏、导航和今天页快捷动作改用内联 SVG，悬停或选中时描线一次。
- 用一个等待组件替换上述五种写法。组件分行内 / 区块 / 整页三档，等待超过 300ms 才显示；现有的具体文案（如“正在读取沟通记录…”）保留。
- Toast 位移从 20px 收到 8px，图标在出现时描线。
- 修改 `DESIGN_WORLD.md`：写入一笔图标、浅灰片规则、等待动画例外和描线时长。

**二期 · 空状态**

- 插画至少覆盖 8 个高频空状态：Inbox 对话、客户最近沟通、待办、搜索无结果、没有客户、本周没有已确认的沟通、联系人、客户文件。更激进的话可以覆盖更多。
- 用统一的 `emptyStateHtml(kind, title, hint, action)` 出图。
- 文案回答三件事：这里是什么、为什么空、下一步做什么，不超过两句。
- 插画第一次出现时描一遍，然后静止。

**三期 · 动作回应与每页点睛**

- 保存时勾一笔（`#actionStatus`）。
- 新沟通记录写入时间线时，下方描一道墨迹再淡去。
- 标记完成时一笔划掉，再收起。
- 每页一处点睛：
  - 今天：焦点客户名下划一笔。
  - Inbox：sela 回复中显示“笔在写”。
  - 客户列表：书眉分段名下描一笔。
  - 客户工作区：“一句下一步”下划一笔。
  - 记录沟通弹窗：等待用区块档，保存后勾一笔。
- 日历、设置、本周工作、操作日志至少换上新图标和空状态。
- 登录柱廊不动。

## 工作方式（项目强制规则，详见 AGENTS.md 与 MULTI_AGENT_WORKFLOW.md）

**开工**

- 会话开始先 `export TRADE_OS_AGENT_ROLE=dev`，再运行 `deploy/cloud/agent-worktree.sh guard`。
- 在桌面端 worktree 里运行 `deploy/cloud/agent-worktree.sh start --task <id> --owner luoxin --goal "..." --scope "..." --base agent/icons-motion-1013` 建任务隔离区。
  - 建议任务 id：`hand-icons-p1`、`hand-empty-p2`、`hand-motion-p3`。
  - 二、三期的基线：基于前一期的分支，或等前一期合入后基于 main。
- 不要进入 `icons-motion-1013` 的隔离区，它属于另一个会话。
- 每个提交以 `[<id>]` 开头。

**验收**

- 在真实浏览器里验收，按 `docs/REAL_BROWSER_REPRO.md` 用 `tools/repro_dev_server.py` 起隔离服务：
  - `--db-dir` 指向 scratchpad 里的空目录。
  - 在 launch.json 的 `env` 里加 `CRM_SEED_DEMO_DATA=1`，就有演示数据。
  - 开发库没有 PIN，在柱廊里点 Hamid 即可进入。
- 宽屏、iPad、iPhone、键盘、200% 缩放、减少动态六种情况各截图。
- 每期都跑：
  - `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v`
  - `node --check app/static/app.js`
  - `python3 tools/check_daylight_contrast.py`
  - `(cd browser-extension && npm test)`
- 然后依次执行 `agent-worktree.sh sync`、`test --task <id>`、`gate --task <id>`。

**收尾**

- 用户能感知的变动写进 `CHANGELOG.md`。
- 开发会话不得 publish。`gate` 显示 ready 后，把准确的发布命令（`deploy/cloud/agent-worktree.sh publish --task <id>`）贴给用户。ECS 是正式环境，发布由用户本人执行。
- 每期结束用 SendUserFile 发前后对比截图（同一页面、同一宽度），配一两句话说明。不必等确认就进入下一期，除非遇到需要人判断的取舍。
- 用户不是 git / 运维背景，汇报用一两句话讲结果，不讲内部细节。
