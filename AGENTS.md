# Trosa：Codex 长期开发速读

## 产品与当前路线

Trosa 是三人使用的外贸 CRM 工作台。核心闭环是：恢复客户上下文 → 执行已确认动作 → 记录实际事实 → 按需确认下一步及日期。沟通记录可以没有下一步；待办必须同时有明确动作和日期。

当前维护路线图是 [`TROSA_MAINTENANCE.md`](TROSA_MAINTENANCE.md)。Customer、Today、Inbox 的“沟通捕获 → 整理 → 人工确认”入口，以及 Customer 工作区的“现在 / 下一步”首屏收敛，已在本地完成并通过回归，未获明确指示不得自行发布 ECS。下一项产品维护优先级是让 Inbox 与 Search 带着上下文进入共同入口；不要跳过路线图直接扩张其他页面。

## 运行与数据边界

- **ECS 是唯一正式运行环境与唯一写入主机**：Flask/Waitress 位于 `/opt/trade-os/current`，SQLite 数据位于 `/var/lib/trade-os`，通过 Cloudflare Tunnel 对外提供服务。
- 本地仅用于开发、隔离测试和已验证发布；不得修改 ECS 数据、正式备份或当前 `data/` 链接所指的运行数据。测试必须使用独立的 `CRM_DB_PATH`。
- 业务数据单一事实源是 SQLite：`system.db` 加上 Hamid、Amy、Kelley 各自独立的数据库。Excel 仅用于导入、导出和历史恢复；Apple 日历仅订阅 ICS，不能回写 CRM。
- 修改前先查看 `git status --short`。工作区可能有用户未提交的改动，绝不覆盖、回退、删除或格式化无关文件。

## 核心模块与不可破坏能力

- 固定核心：客户、联系人、沟通时间线、待办/Today、Inbox、Search、日历、归档恢复、导入导出与备份。
- 未配置 AI 时，上述核心必须完整可用；核心页面不能等待 AI、网站读取、邮箱验证或后台任务。
- 写入必须绑定已认证用户；跨用户读取只可返回白名单字段。客户/联系人匹配只允许规范化后的唯一精确邮箱或手机号自动确认，其余只能进入候选/待审阅。
- 保存后优先局部更新。大列表分页或按需加载；客户详情先加载摘要和最近记录，完整时间线、联系人、文件按需加载。
- 所有核心页面要区分加载、空、未登录、权限、网络、服务与解析错误，不能把失败显示成“没有数据”。

## Sela 与 AI

- Sela 通过受限 `/api/integrations/sela/*` 接口同步**已确认**外联，使用精确身份匹配、幂等键和 `REVIEW` 处理歧义；不得直接读写 SQLite。
- AI/Sela 应预填来源、时间、渠道、证据、摘要和候选，优先减少手工操作；不能自动创建客户、联系人、待办或商业承诺。
- 没有足够信息时宁可进入 Inbox/待审阅，也不要自动执行高风险业务动作。发送消息、报价、价格/交期承诺始终由人完成。

## 已冻结与不应扩张的内容

- 网站监控、客户 AI 研究/推荐不再是活动用户模块；历史数据保留。
- 15/30/60 天自动开发节点保持兼容，但不能重新进入 Today。
- 不恢复独立 Pi 前端运行时；保留的 `/api/agent/*` 只能做原子读取和确认式提案。
- 不重新引入大量客户状态、等级、标签、复杂视图或批量管理作为前台工作负担；历史字段和接口暂不删除。

## 修改原则

- 不因 `app.py` 或 `app/static/app.js` 很大就进行拆文件或大规模重构。先完成一个完整、可验证的问题，复用现有端点和数据模型。
- 不恢复冻结功能，除非用户明确要求。
- 任何功能改动都不得破坏客户、联系人、沟通记录、Today、Inbox、Search、备份恢复或 Sela 的幂等同步契约。
- 涉及 UI 时先读 `design/TRADE_OS_UI_SYSTEM.md`、`design/inspiration/DESIGN_PROFILE.md` 和相关 `CATALOG.md`；真实浏览器验收宽屏、iPad、iPhone、旧 Win10、键盘、200% 缩放与性能模式。
- 写入、导入、附件、备份、恢复必须保留来源、校验与审计；删除必须有明确目标、确认与可恢复快照。

## 开发与验证

首次或依赖变更后：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd browser-extension && npm install
```

修改前至少保存当前状态；修改后按影响范围运行：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m py_compile app.py db.py scheduler.py
node --check app/static/app.js
(cd browser-extension && npm test)
```

后端、前端、数据迁移、性能和发布还须遵循 `TROSA_MAINTENANCE.md` 的最低验证与 ECS 发布检查。用户可感知的功能变动同步记录到 `CHANGELOG.md`；仅开发环境或开发文档变动无需伪造产品变更日志。
