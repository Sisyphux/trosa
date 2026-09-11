# Trade OS

Trade OS 是三人使用的外贸客户关系工作台。它围绕客户、联系人、沟通记录和明确待办组织日常工作，帮助团队保存关系上下文、看见到期承诺并记录实际进展。

## 当前架构

```text
浏览器 / 桌面窗口
        ↓
Flask 应用（app.py）
        ↓
业务规则与接口
        ↓
ECS PostgreSQL（正式唯一业务写入源，127.0.0.1:5432）
        ├─ identity / core / trosa / sela / audit
        └─ 兼容层：保留旧 SQLite 形状的 API，不再创建个人 SQLite 主库
        ↓
/var/lib/trade-os/uploads/  客户附件与导入来源
```

- 核心闭环：客户与联系人 → 已确认沟通事实 / 邮件证据 → 明确待办 → 到期执行 → 新记录。
- 核心页面：今天、Inbox、客户、本周工作；完整日历、全部记录和操作日志从场景入口进入。
- 数据存储：正式 ECS 使用 PostgreSQL 作为业务数据唯一事实源；SQLite 仅用于隔离开发、历史导入/演练和明确批准的回滚材料。Excel 用于导入、导出和历史恢复。
- 数据保护：正式备份由 PostgreSQL logical dump 加客户附件 bundle 组成，并在独立位置做 SHA-256 与 restore-check；应用不会在 PostgreSQL 模式下伪造 SQLite 快照。恢复前先保存当前版本，任何时刻只允许一个写入源。
- Apple 日历：通过个人 ICS 订阅读取待办，只同步日历事件，不复制 CRM 数据库。
- AI：可关闭的按需辅助模块，用于整理、分析和问答。关闭或未配置模型时，客户、记录、待办、Inbox、日历、导入导出和备份恢复保持完整可用。
- iCloud：当前运行链路不包含 iCloud 数据库拉取、推送或冲突合并。

### 事实与兼容边界

- `trosa.customer_states` 是 `business_stage`、`business_role` 与 `customer_judgment` 的正式 PostgreSQL 事实源；旧 `status`、`type`、`attention_*` 仅为导入、恢复和历史兼容保留。
- `follow_up_logs` 是已记录沟通事实，`outreach_emails` 是发送、投递和回复证据；它们通过同一客户事实投影提供联系状态、最近沟通、等待回复和下一步。
- Today 只读取有动作和日期的开放任务；`outreach_*` 自动开发节点、网站监控和旧 AI 研究不参与正式运行路径。
- “未建立真实沟通”是客户列表筛选，不是第二个 New Customer 数据域；已发送但未获回复的开发邮件仍是邮件证据，不能被误写成客户已联系。

## 项目结构

```text
app.py                 Web 应用、接口与业务编排
db.py                  PostgreSQL 运行入口、SQLite 兼容/演练、迁移与来源审计
app/engine.py          可选的模型调用与网站内容处理
scheduler.py           到期提醒、可选监控和后台任务
ical_gen.py            个人 ICS 日历订阅
maintenance.py         存储检查与维护
email_verifier.py      可选的邮箱可发送性后台复核
app/static/            单页前端
tests/                 风险回归测试
deploy/                Linux 服务与 Tunnel 示例
design/                视觉系统与原型
archive/               历史发布包
```

## 文档职责

| 文件 | 用途 |
|---|---|
| `PRODUCT_DIRECTION.md` | 当前产品边界与能力优先级 |
| `PRODUCT_DESIGN_STANDARD.md` | 产品、文案、交互和验收规则 |
| `Trade OS 系统设计说明.md` | 信息模型、数据流和自动化边界 |
| `使用说明.md` | 启动、日常使用、数据保护和可选配置 |
| `DEPLOYMENT.md` | 家庭服务器上线与备份要求 |
| `CHANGELOG.md` | 已落地变更与验证结果 |

`Trade OS 重构审查报告.md` 和 `Trade OS AI 设计原则与功能边界.docx` 记录特定阶段的审查背景，供追溯决策使用；当前规则以上表中的现行文档为准。

## 启动与验证

Windows 可双击 `start.bat`。也可以在已安装依赖的环境中运行：

```bash
.venv/bin/python app.py
```

默认访问地址为 `http://localhost:8080`。改动后至少执行：

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
.venv/bin/python -m py_compile app.py db.py scheduler.py
node --check app/static/app.js
(cd browser-extension && npm test)
```

首次在本机开发时，创建 Python 虚拟环境并安装固定依赖；浏览器扩展测试也需安装其锁定的开发依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
(cd browser-extension && npm install)
```
