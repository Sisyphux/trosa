# Agent 访问业务数据：开箱即用指南

> 开发/审查/分析 Agent 想读客户、沟通、待办，或在成员明确指令下直接写入时，读这一页。
> 实现：`tools/trosa_cli.py`（纯标准库，无需安装依赖）→ 受限接口 `/api/gateway/*`（写入）与 `/api/agent/*`（只读）。

## 一句话原则：可逆即自由

- **可逆的动作，Agent 可以直接做**：新增、修改、调整日程、归档 / 恢复、批量。每一次写入都返回 `action_id`，用 `undo <action_id>` 原样撤销；相同请求重试不会重复落地（幂等）。
- **不可逆或对外的动作，永远由人完成**：永久删除、对外发送消息、报价、价格与交期承诺。
- **出错了怎么 undo**：任何写命令的输出里都有 `undo_hint`，直接照抄执行：

  ```bash
  python3 tools/trosa_cli.py undo <action_id>
  ```

  `command` 失败不会留下半截数据：单条写入要么整体成功，要么整体回滚；`batch` 里任一子动作失败会按相反顺序撤销已应用项，等于整体不落地。

## 能做什么、不能做什么

| 能力 | 令牌权限 | 命令 | 可逆性 |
|---|---|---|---|
| 读客户、联系人、沟通、待办、Today、Inbox、客户工作区、完整时间线、跨客户消息搜索 | `crm:read` | `customers` `customer` `contacts` `activity` `tasks` `today` `inbox` `snapshot` `workspace` `timeline` `search` | 只读，仅限令牌所属成员的数据；跨成员只返回白名单字段，令牌不能指定 `user_id` |
| 提交“待确认”的建议 | `crm:propose` | `propose-task` `propose-communication` | 低：提议不直接写入，成员在 Trosa 里确认 |
| 直接写入（成员明确指令下） | `crm:write` | `create-customer` `update-customer` `create-contact` `update-contact` `record-communication` `create-task` `update-task` `complete-task` `reschedule` `archive-customer` `restore-customer` `batch` `undo` | 中：每条都带幂等键、审计与撤销；`batch` 一次最多 50 个子动作且整体可撤销 |
| 永久删除 / 高风险 | 无 | 无 | 任何 scope 都不放行：`delete_customer` `delete_contact` `delete_task` `delete_timeline` `delete_inbox` `delete_attachment` `bulk_update` `restore_database` `manage_tokens` 一律 409 |

默认推荐令牌：`crm:read` + `crm:write`（`issue-token` 的默认值）。写动作只做“可逆”的事，删除类动作在网关的动作策略表（`app.py` 的 `_GATEWAY_ACTION_POLICY`）里被登记为 `reversible=False`，任何 scope 都不会执行；表里没有的动作同样一律拒绝。

不能做的事：读别的成员的数据、永久删除、对外发送消息或承诺价格 / 交期、读取发布凭据（`~/.config/trosa/workbench.env` 与令牌无关，也不要碰）、用浏览器或 Computer Use 读业务数据。

## 领取令牌（成员本人，一次性，约 1 分钟）

令牌创建需要成员本人的访问码，**不要把访问码给 Agent，也不要在 Agent 会话里运行下面的命令**（它在没有交互式终端时会拒绝运行）。在你自己的终端：

```bash
python3 tools/trosa_cli.py issue-token --user <你的账号> --label followup-review
# 默认 scopes=crm:read,crm:write，默认有效期 90 天；也可显式指定：
#   --scopes crm:read,crm:propose            （只读 + 提议）
#   --expires-days 30
```

令牌只写入 `~/.config/trosa/agent.token`（权限 600），不会显示在屏幕或聊天里。之后所有 Agent 会话自动使用它；也可以用环境变量 `TROSA_AGENT_TOKEN` 覆盖，用 `TROSA_BASE_URL` 指向别的环境（默认 `https://app.trosa.space`）。

令牌默认 90 天过期。`whoami` 在剩余不足 7 天时会给出 `renew_hint`，提醒成员本人重新领取。用完或怀疑泄露时撤销：

```bash
python3 tools/trosa_cli.py revoke-token --user <你的账号> list     # 查看 id
python3 tools/trosa_cli.py revoke-token --user <你的账号> <id>
```

## 常用流程

**整体跟进审查**（客户跟进是否太空、该排哪些日期）：

```bash
python3 tools/trosa_cli.py snapshot                          # 全部客户 + 全部未完成待办；customers_without_open_task 即没有下一步的客户
python3 tools/trosa_cli.py activity --customer <id> --all    # 钻进某个客户的沟通记录
python3 tools/trosa_cli.py workspace <id>                    # 客户工作区（承诺、最近事实、信息缺口）
python3 tools/trosa_cli.py search --query 报价               # 跨客户消息搜索
```

**直接写入**（仅在成员明确指令下）：

```bash
python3 tools/trosa_cli.py create-customer --name "ACME" --company "ACME Co" --email buyer@acme.test
python3 tools/trosa_cli.py update-customer 12 --notes "已寄样"
python3 tools/trosa_cli.py create-task 12 --title "跟进样品反馈" --due 2026-10-08
python3 tools/trosa_cli.py reschedule 34 --date 2026-10-15
python3 tools/trosa_cli.py archive-customer 12        # 归档到回收站，可 restore-customer 恢复
python3 tools/trosa_cli.py restore-customer 12
python3 tools/trosa_cli.py undo <action_id>           # 撤销上面任意一次写入
```

写命令输出里带 `action_id`（在 `data.action.id`）与 `undo_hint`。`create-customer` 命中已有客户的唯一精确邮箱 / 手机号时不写库，而是返回 `duplicate_candidate`（含已有客户 `customer_id`），不自动合并、不重复创建。

**批量**（一次最多 50 个可逆子动作，整体成功或整体不落地；成功后一个 `action_id` 撤销整批）：

```bash
python3 tools/trosa_cli.py batch actions.json          # 传文件
echo '[{"action":"create_task","customer_id":12,"payload":{"title":"寄样","due_date":"2026-10-08"}}]' \
  | python3 tools/trosa_cli.py batch                   # 或读 stdin
```

## 数据与分页约定

- 所有输出为 JSON；列表接口单页最多 50 条，用 `--offset` 翻页或直接加 `--all`。
- “没有结果”不等于现实中没发生沟通：记录来自 Trosa 已录入的数据（接口返回里带 `fact_policy` 的就是这个提醒）。
- 令牌对 `/api/gateway/*` 与 `/api/agent/*` 生效，且对 `/api/agent/*` **只在 GET 且持有 `crm:read` 时**通过；`/api/agent/*` 的写方法（如提交提议）与其它网页登录接口对令牌一律 403 / 401。附件只开放元数据列表，不开放文件内容下载。

## 退出码

`0` 成功 · `1` 服务端/参数错误 · `2` 用法错误或没有令牌 · `3` 网络错误 · `4` 令牌无效、已撤销、已过期或权限不足。

## 给维护者

- 网关写动作的唯一事实源是 `app.py` 的 `_GATEWAY_ACTION_POLICY`：表里没有的动作一律 409，`reversible=False` 的动作任何 scope 都不放行。新增动作必须同时登记处理器与撤销路径，否则 `tests/test_agent_data_freedom.py` 会失败。
- 接口契约与幂等回执在 `app.py` 的 `gateway_*` 路由；测试在 `tests/test_agent_cli.py`（CLI，离线）、`tests/test_agent_data_freedom.py`（策略表、写入、读放开、CLI 端到端）与 `tests/test_risk_regressions.py`（`agent_gateway` 相关用例）。新增网关读接口时，同步在 CLI 里加命令并更新本页。
