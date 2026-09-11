# Trosa Agent API 当前能力审计

> 审计日期：2026-08-30；本文随后按 2026-09-10 的清理结果校正。
> 范围：Trosa REST API、当前 `/api/agent/*` 原子读取/提议、`/api/gateway/*` 受限 Agent 入口、Sela 集成认证、Today、Inbox、Search 与统一沟通确认路径。
> 本文记录当前能力边界；仓库不再包含独立 Agent/MCP/Web runtime。数据库迁移和历史数据兼容不等于重新开放旧运行入口。

## 结论

当前正式边界是：

```text
Sela / 其他 Agent
       ↓              ↓
受限 Sela API      /api/gateway/*（Bearer token）
       ↓              ↓
          Trosa 受控业务 API
                  ↓
      Customers / Timeline / Today / Inbox / Search
```

不需要恢复 Agent 直读 SQLite、文件夹或独立聊天运行时。当前本地 `/api/agent/*` 面向已登录 Trosa 会话，提供有界读取和需确认的提议；`/api/gateway/*` 面向外部 Agent，使用个人 Bearer token、scope、幂等写入和可撤销动作。两者都复用 Trosa 的受控业务函数，Sela 仍走独立的受限集成接口。

## 现有安全边界

- `get_db()` 在 SQLite 隔离/回滚边界内按当前认证用户路由；正式运行由 PostgreSQL 组织/成员作用域隔离。
- 普通业务 API 和 `/api/agent/*` 使用 `login_required`；`/api/gateway/*` 使用个人 Bearer token、scope 和当前用户绑定。
- Sela 使用受限 Bearer token 集成；prospects/exclusions 为 `sela-v2`，follow-up 为 `sela-follow-up-v1`，并具有事务、精确身份匹配、`REVIEW` 和幂等收据。
- Gateway 的低风险写入使用 `Idempotency-Key`，记录 `agent_actions`，并通过 undo token 提供冲突感知撤销；高风险操作不允许直接执行。
- 统一沟通入口与 Gateway 的 `record_communication` 共同复用当前业务函数，能记录事实、完成匹配的到期待办、按需创建下一待办，并仅在成功后解决指定 Inbox 条目。

这些能力说明不需要更换数据模型，也不需要让 Gateway 接触数据库。

## 当前 Agent 能力映射

| Agent 能力 | 当前入口 | 当前边界 |
|---|---|---|
| `search_customers` | `GET /api/gateway/customers`；登录会话仍可使用 `/api/customers?search=...` | 只返回客户基础投影和有界结果，不接受 Agent 切换用户或扩大字段范围。 |
| `get_customer` | `GET /api/gateway/customers/<id>` 与 `/api/agent/customers/<id>/workspace` | 先读客户受限摘要，再按需读取联系人、时间线和任务；不把旧研究字段当作当前工作区。 |
| `get_today` | `GET /api/gateway/today` 与 `/api/agent/brief/today` | 只返回当前人工待办；历史 `outreach_%` 行和退役 Inbox 类型不进入 Today/brief。 |
| `search_activity` | `GET /api/gateway/activity` 与 `/api/agent/messages/search` | 结果来自已记录沟通/开发信；有界 limit，空结果不等于现实中没有发生沟通。 |
| `record_communication` | `POST /api/gateway/actions` 的 `record_communication`；登录会话可用 Agent activity proposal | Gateway 使用 `crm:write`、`Idempotency-Key` 和共享沟通写入；Agent proposal 仍需用户确认。 |
| `create_task` | `POST /api/gateway/actions` 的 `create_task`；客户任务 API 与 Agent task proposal | 待办必须同时有动作和日期；直接 Gateway 写入可撤销，proposal 写入需确认。 |
| `complete_task` | Gateway `complete_task`；登录会话的共享完成路径 | 完成动作可带实际沟通和下一步；不直接恢复退役自动开发节点。 |
| `get_inbox` | `GET /api/gateway/inbox`、`GET /api/agent/brief/today` | 只暴露当前客户回复、浏览器/Gmail 采集和 Sela 请求等待判断条目。 |
| `resolve_inbox` | Gateway `resolve_inbox` / `assign_inbox_customer`；网页 `archive` 与共同沟通入口 | 不再提供旧 snooze/建议解析入口；客户回复只有在沟通事实成功写入后才解决。 |
| `get_recent_activity` | `GET /api/gateway/actions/recent`、`/api/gateway/activity` 和客户 timeline | 有界读取、绑定当前 token 用户，不另建一套活动数据源。 |

当前外部 Gateway 已将读取与写入按 scope 分开；`/api/agent/*` 保留为登录会话下的原子读取和确认式提议。新增工具应复用这些业务入口，不再增加第二套 CRM 规则。

## 当前写入路径

当前 Gateway 的低风险 `record_communication`、`create_task`、`complete_task`、客户/联系人更新和 Inbox 处理均通过共享业务函数执行，并在 `agent_actions` 中留下可撤销动作；重复请求由 `agent_gateway_idempotency` 按 `Idempotency-Key` 重放原结果。登录会话下的 `/api/agent/proposals/<id>/confirm` 也调用同一批共享函数，仍保留人工确认边界。

高风险删除、批量更新、恢复数据库和 token 管理不允许通过 Gateway 直接执行。历史自动开发行和退役 Inbox 类型即使仍在数据库中，也不会被这些当前写入路径重新操作。

## Gateway 认证与权限

不要复用浏览器 PIN、Session cookie，也不要把现有 Sela token 扩权。

每位用户可以在 Trosa 中创建独立的 Agent Gateway token，服务端只保存 SHA-256 摘要，并把 token 映射到唯一用户。请求进入 Flask 后绑定该用户的 PostgreSQL 组织作用域（SQLite 仅作为隔离/回滚兼容边界）。

当前 scope 为：

- `crm:read`：客户、Today、Inbox、Search、Timeline 等有界读取；
- `crm:propose`：创建待确认的业务提议；
- `crm:write`：执行低风险、可幂等、可撤销的业务动作。

高风险操作仍不能由外部 Agent 直接调用。Sela token 不与 Gateway token 共用，也不扩权。

## 提议与幂等

当前 `agent_proposals` 保存 `task`、`activity` 及 Sela follow-up 提议，Gateway 的直接动作另由 `agent_actions` 和幂等表记录：

- Agent proposal 提交前校验客户、动作、日期和来源；登录会话可读取、编辑、确认或取消待确认提议；确认时重新读取当前业务状态。
- Gateway 写入使用 `Idempotency-Key`；相同 key 和请求重放原结果，不同请求返回冲突；动作保留 `undo_token`。
- 当前 Inbox 处理只有归档、归属确认、沟通事实写入后的解决等有限语义；不再把 snooze 或旧建议解析作为契约。
- Gateway token 的创建/撤销仍是登录会话管理能力，不属于外部 Agent 的 `crm:write`。

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

当前维护顺序：

1. 保持 Inbox 与 Search 通过共同确认入口进入同一套事实写入；
2. 保持 `/api/agent/*` 的登录会话提议与 `/api/gateway/*` 的外部动作复用共享业务函数；
3. 持续验证 token scope、幂等、撤销、歧义匹配、Inbox 一致性和核心无 AI 回归；
4. 新增协议适配只能复用当前 Gateway/Sela 合同，不在 Trosa 内恢复独立 runtime 或第二套 CRM 规则。

Sela 证据交接仍按路线图任务 4 进行。Local Bridge 单独立项，只负责本地文件搜索、指定文件读取和摘要，通过出站 HTTPS 返回受限结果；它不持有 CRM 数据库权限，也不应被合并进 Trosa Agent Gateway。

## 第一阶段验收标准

- 外部 Agent token 只能访问绑定用户和白名单工具，不能读取其他用户数据库；
- 无 token、错误 scope、过期/撤销 token 均返回明确错误；
- 搜索歧义只返回候选，不自动确认客户归属；
- Agent proposal 创建后，CRM 业务数据保持不变，直到用户在 Trosa 确认；Gateway 低风险直接动作必须可幂等、可审计、可撤销；
- 同一个外部请求重放不会创建第二条业务事实；
- Agent 沟通确认、Gateway 写入与网页统一确认产生一致的 Timeline、Today、Inbox 和客户摘要结果；
- 待办始终同时有明确动作和日期；沟通记录允许没有下一步；
- 未配置 AI 或 Gateway 时，客户、沟通、Today、Inbox、Search、导入导出和备份恢复继续完整可用；
- 全部测试使用独立 `CRM_DB_PATH`/隔离数据库，不修改 ECS、正式数据库或正式备份。

## 本次核验

本次审计在隔离数据库中运行并通过以下现有回归：

- Agent 今日简报、客户 workspace 与确认式待办提议；
- Agent timeline 与沟通搜索的组合和未登录拦截；
- Gateway 读取、scope、幂等写入、undo 与高风险动作拦截；
- 统一沟通写入成功后只解决指定 Inbox 回复。

这些测试证明现有原子能力可复用，但尚未覆盖 Gateway token、外部幂等、proposal 恢复、Agent 沟通确认与统一写入等本审计指出的新增边界。
