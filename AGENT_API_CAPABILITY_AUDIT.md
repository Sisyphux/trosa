# Trosa Agent API 能力审计

> 审计日期：2026-08-30  
> 范围：当前本地工作树中的 Trosa REST API、`/api/agent/*`、Sela 集成认证、Today、Inbox、Search 与统一沟通确认路径。  
> 本文只做能力审计和最小接口规划，不实现 Agent、MCP Server、Local Bridge，不修改 ECS 或正式数据。

## 结论

推荐采用以下边界：

```text
Sela / Pi / Codex / 其他 Agent
              ↓
       Thin Agent Gateway
              ↓
      Trosa 受控业务 API
              ↓
 Customers / Timeline / Today / Inbox / Search
```

方向正确，而且不需要恢复 Agent 直读 SQLite 或文件夹。

当前 Trosa 的**底层业务能力约有 8/10 已存在**：客户搜索、客户工作区、Today、Inbox、沟通搜索、时间线、沟通写入、待办创建和待办完成都有现成逻辑。真正缺少的不是 Agent，而是一个可供外部调用的、按用户绑定且有明确权限边界的 Gateway 契约。

目前还不应直接把现有 `/api/agent/*` 暴露给远程 Agent，主要原因有三项：

1. `/api/agent/*` 只接受浏览器登录 Session，没有独立的 Agent Bearer token、用户绑定和工具级 scope。
2. Agent 的沟通提议确认使用独立 SQL 写入，没有复用统一确认面板背后的完整 `follow_history` 业务规则。
3. 外部重试、提议查询、分页、紧凑字段和 Inbox 处理语义还未形成稳定契约。

因此下一步应是**补一层很薄的 Agent API，而不是写 Agent Runtime**。

## 现有安全边界

- `get_db()` 已按当前认证用户路由到 Hamid、Amy、Kelley 各自独立的 SQLite，用户隔离的基础是可复用的。
- 普通业务 API 和 `/api/agent/*` 都有 `login_required`，未登录读取已由回归测试覆盖。
- Sela 已证明“Bearer token → 固定用户 → 端点白名单”模式可行；其同步还具有事务、精确身份匹配、`REVIEW` 和幂等收据。
- 待办创建、修改、取消以及部分 Agent 确认写入已有冲突感知的撤销快照。
- 统一沟通入口 `POST /api/customers/<id>/follow_history` 已是当前最完整的沟通写入终点：它能记录事实、完成匹配的到期待办、关闭旧开发节点、按需创建下一待办、更新客户状态/理解，并仅在成功后解决指定 Inbox 条目。

这些能力说明不需要更换数据模型，也不需要让 Gateway 接触数据库。

## 10 个候选工具的能力映射

| Agent 工具 | 现有能力 | 可复用程度 | 开放前最小补充 |
|---|---|---:|---|
| `search_customers` | `GET /api/customers?search=...`，已有分页、自然语言筛选、联系人/沟通/待办/Inbox 命中和 `match_context` | 高 | 增加 Agent 专用紧凑字段投影；默认不返回完整客户行、备注和无关联系人隐私；返回明确的 `exact/candidate/ambiguous` 匹配状态 |
| `get_customer` | `GET /api/customers/<id>/summary` 与 `GET /api/agent/customers/<id>/workspace` | 高 | 以 summary 为默认；联系人、完整时间线和开发信按需读取；避免 workspace 的 `SELECT *` 成为默认响应 |
| `get_today` | `GET /api/agent/brief/today` 与 `GET /api/reminders/today` | 高 | 统一 Today 的排序与字段；不要用 Agent brief 当前的 Inbox 直查代替真实 Inbox 生成逻辑；增加上限/分页 |
| `search_activity` | `GET /api/agent/messages/search` | 高 | 保留关键词、国家、方向、日期、客户和回复状态过滤；增加游标分页和稳定 schema；明确“无结果不等于现实未发生” |
| `record_communication` | Agent `activity` proposal；统一 `follow_history` 写入 | 中 | 工具只创建草稿/提议，不直接写入；用户确认时必须调用共享的 `follow_history` 业务服务，不能继续走当前独立 SQL |
| `create_task` | Agent `task` proposal；`POST /api/customers/<id>/tasks` | 高 | 工具只创建提议；确认后复用共享待办创建服务；校验动作与日期；外部重试须幂等 |
| `complete_task` | `PUT /api/reminders/<id>` | 中 | 新增 `task_completion` 提议类型；完成待办必须同时确认实际发生了什么，可选确认下一步及日期；不能只静默把 `is_done` 改为 1 |
| `get_inbox` | `GET /api/inbox` | 高 | 提供紧凑、分页、可解释的 Agent 视图，保留 `why_now/evidence/source/status`；不能重新生成一套 Inbox 判断规则 |
| `resolve_inbox` | `archive`、`snooze`、`resolve-suggestion`、`follow_history(inbox_item_id)` 等分散端点 | 中低 | 定义有限动作枚举：`record_fact`、`snooze`、`archive`、`keep_waiting`；均先形成确认提议；客户回复只能在沟通写入成功后解决 |
| `get_recent_activity` | `messages/search` 在空 query 下可返回最近沟通和开发信；客户 timeline 也有有界读取 | 高 | 增加稳定的 recent 入口或把它定义为 `search_activity` 的预设；支持 `since/cursor/limit`，默认只返回必要字段 |

建议最终对 Agent 暴露 9 个工具：把 `get_recent_activity` 合并为 `search_activity` 的空查询/时间预设，减少重复契约。如果上层 MCP 客户端更需要显式工具名，也可以保留 10 个，但底层仍调用同一读取函数。

## 必须先修的业务分叉

当前 `POST /api/agent/proposals/<id>/confirm` 对 `activity` 提议直接插入 `follow_up_logs`。这条路径只更新 `last_contact`，没有完整执行统一沟通入口的规则，至少会漏掉：

- 自动完成沟通日期之前的匹配到期待办；
- 关闭同客户遗留的 15/30/60 天开发节点；
- 创建提议中的下一步动作与日期；
- 更新 `next_follow_up`、`manual_next_follow`、客户类型和跟进状态；
- 刷新当前状态与工作理解；
- 仅在写入成功后解决关联 Inbox 条目；
- 返回统一确认面板用于局部刷新的完整结果。

这会让“网页确认”和“Agent 确认”产生两个业务事实版本，是当前最高优先级缺口。

最小修法不是让一个 Flask route 内部 HTTP 调另一个 route，而是把以下三段现有逻辑提取为小型业务函数，并让网页 API 与 Agent 确认共同调用：

1. `record_communication_for_user(...)`
2. `create_task_for_user(...)`
3. `complete_task_with_activity_for_user(...)`

只提取这三个闭环，不拆分整个 `app.py`，不改变表结构和核心业务语义。

## Gateway 认证与权限

不要复用浏览器 PIN、Session cookie，也不要把现有 Sela token 扩权。

建议为每位用户创建独立的 Agent Gateway token，服务端只保存 SHA-256 摘要，并把 token 映射到唯一用户。请求进入 Flask 后仍通过现有 `set_db_user(user)` 和 `get_db()` 路由到该用户数据库。

建议 scope 只有三类：

- `agent:read`：客户、Today、Inbox、Search、Timeline；
- `agent:propose`：创建待确认草稿；
- `agent:confirm`：**不授予外部 Agent**，只允许已登录的 Trosa 浏览器会话执行。

关键约束：Agent 可以准备 `record_communication`、`create_task`、`complete_task` 和 `resolve_inbox`，但不能自己调用最终确认。Gateway 返回 `proposal_id` 和可在 Trosa 打开的确认入口，统一面板加载提议、展示来源/客户/日期/事实/下一步，用户确认后由浏览器 Session 完成写入。

## 提议与幂等

现有 `agent_proposals` 能保存 `task` 和 `activity`，但 Gateway 化之前需要补齐契约：

- 支持 `activity`、`task`、`task_completion`、`inbox_resolution` 四类提议；
- 提议携带来源、外部请求 ID、关联 Inbox/待办 ID、规范化后的预填字段和原始证据摘要；
- 增加读取单条提议和读取当前用户 pending 提议的 Session API，供统一确认面板恢复上下文；
- 创建提议使用 `X-Idempotency-Key`，相同 key + 相同请求返回原结果，相同 key + 不同请求返回 409；
- confirm/cancel 重试返回原终态，不应把网络重试变成含混的 404；
- 确认时重新读取客户、待办和 Inbox 当前状态，发生变化时返回 409 并要求刷新，而不是覆盖新事实。

不一定需要新表。现有每用户数据库中的 `integration_sync_receipts` 已是通用的幂等收据结构，可以用新的 integration 名称复用；新增字段若只用于展示，也可先放在受严格校验的 proposal payload 中。

## 响应边界与隐私

现有客户搜索使用 `SELECT *`，客户 workspace 也返回完整客户行、全部联系人和最近开发信内容。这适合登录后的重型工作区，不适合作为 Agent 搜索的默认结果。

建议 Agent 响应遵循两段式读取：

1. 搜索只返回 `id/name/company/country/primary_contact_hint/last_activity/next_step/match_context`；
2. 上层明确选择 `customer_id` 后，`get_customer` 才返回该客户的受限工作区；完整 timeline 再单独按需读取。

所有列表必须有 `limit` 上限和游标；所有错误必须区分 401、403、404、409、429 和 5xx；空数组只表示“当前查询没有记录”，不能把权限、网络或解析失败伪装成“没有数据”。

## 关于“最近一周哪些客户一直没有回复”

现有能力可以回答一部分，但必须按证据类型区分：

- `outreach_emails.reply_status` 可以明确判断开发信的 `pending/no_reply/replied/bounced`；
- 普通 `follow_up_logs` 只有沟通方向和事实记录，没有稳定的“某次我方消息对应哪次客户回复”关系，不能精确声称某条普通消息一直未回复；
- 客户的等待状态、最近 outbound/inbound 事实和 Inbox 信号可以生成“可能仍在等待回复”的候选，但答案必须说明这是基于 CRM 已记录事实的推断。

因此第一版 Agent 应回答“CRM 中有明确未回复证据的客户”和“基于最近记录推断仍在等待的客户”两组，不能混为一个确定结论。

## 与当前维护路线图的顺序

Agent API 的优先级可以提高，但不应跳过路线图任务 3。

推荐顺序：

1. 先完成 Inbox 与 Search 带上下文进入统一确认入口；
2. 提取三个最小共享业务函数，消除 Agent 确认的独立写入分叉；
3. 增加按用户绑定的 Agent Gateway token、scope 和端点白名单；
4. 稳定 9–10 个工具的紧凑 JSON schema、分页、错误与幂等；
5. 让 proposal 能在 Trosa 统一确认面板中恢复、编辑、确认和取消；
6. 完成三用户隔离、重复请求、歧义匹配、撤销冲突、Inbox 一致性和核心无 AI 回归；
7. 最后再实现 MCP Server、Sela tools 或聊天入口，它们只做协议适配，不再包含 CRM 业务规则。

Sela 证据交接仍按路线图任务 4 进行。Local Bridge 单独立项，只负责本地文件搜索、指定文件读取和摘要，通过出站 HTTPS 返回受限结果；它不持有 CRM 数据库权限，也不应被合并进 Trosa Agent Gateway。

## 第一阶段验收标准

- 外部 Agent token 只能访问绑定用户和白名单工具，不能读取其他用户数据库；
- 无 token、错误 scope、过期/撤销 token 均返回明确错误；
- 搜索歧义只返回候选，不自动确认客户归属；
- Agent 创建沟通/待办/完成/Inbox 处理时，CRM 业务数据保持不变，直到用户在 Trosa 确认；
- 同一个外部请求重放不会创建第二个 proposal 或第二条业务事实；
- Agent 沟通确认与网页统一确认产生完全一致的 Timeline、Today、Inbox 和客户摘要结果；
- 待办始终同时有明确动作和日期；沟通记录允许没有下一步；
- 未配置 AI、Gateway 或 MCP 时，客户、沟通、Today、Inbox、Search、导入导出和备份恢复继续完整可用；
- 全部测试使用独立 `CRM_DB_PATH`/隔离数据库，不修改 ECS、正式数据库或正式备份。

## 本次核验

本次审计在隔离数据库中运行并通过以下现有回归：

- Agent 今日简报、客户 workspace 与确认式待办提议；
- Agent timeline 与沟通搜索的组合和未登录拦截；
- Agent command 的读取、提议、确认与取消；
- 统一 `follow_history` 写入成功后只解决指定 Inbox 回复。

这些测试证明现有原子能力可复用，但尚未覆盖 Gateway token、外部幂等、proposal 恢复、Agent 沟通确认与统一写入等本审计指出的新增边界。
