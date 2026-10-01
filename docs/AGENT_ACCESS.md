# Agent 访问业务数据：开箱即用指南

> 开发/审查/分析 Agent 想读客户、沟通、待办，或提交“待确认”的建议时，读这一页。
> 实现：`tools/trosa_cli.py`（纯标准库，无需安装依赖）→ 受限接口 `/api/gateway/*`。

## 一句话

先看自己有没有令牌：

```bash
python3 tools/trosa_cli.py whoami
```

通过就直接干活；失败（退出码 2）说明还没领令牌，请**成员本人**按下面“领取令牌”做一次，Agent 不要自己想办法绕过。

## 能做什么、不能做什么

| 能力 | 令牌权限 | 命令 | 风险 |
|---|---|---|---|
| 读客户、联系人、沟通记录、待办、Today、Inbox | `crm:read` | `customers` `customer` `contacts` `activity` `tasks` `today` `inbox` `snapshot` | 只读，仅限令牌所属成员的数据 |
| 提交新待办 / 沟通记录提议 | `crm:propose` | `propose-task` `propose-communication` | 低：提议只是“待确认”，成员在 Trosa 里确认才生效；相同请求重试不会重复 |
| 直接写入 | `crm:write` | 无（CLI 刻意不提供） | 中：仅在成员明确要求且按任务临时开启时使用，可 undo |

默认推荐令牌：`crm:read` + `crm:propose`。删除、批量改、恢复数据库、管理令牌在任何权限下都不可用。

不能做的事：读别的成员的数据、改日程而绕过确认、读取发布凭据（`~/.config/trosa/workbench.env` 与它无关，也不要碰）、用浏览器或 Computer Use 读业务数据。

## 领取令牌（成员本人，一次性，约 1 分钟）

令牌创建需要成员本人的访问码，**不要把访问码给 Agent，也不要在 Agent 会话里运行下面的命令**（它在没有交互式终端时会拒绝运行）。在你自己的终端：

```bash
python3 tools/trosa_cli.py issue-token --user <你的账号> --label followup-review --scopes crm:read,crm:propose
```

令牌只写入 `~/.config/trosa/agent.token`（权限 600），不会显示在屏幕或聊天里。之后所有 Agent 会话自动使用它；也可以用环境变量 `TROSA_AGENT_TOKEN` 覆盖，用 `TROSA_BASE_URL` 指向别的环境（默认 `https://app.trosa.space`）。

用完或怀疑泄露时撤销：

```bash
python3 tools/trosa_cli.py revoke-token --user <你的账号> list     # 查看 id
python3 tools/trosa_cli.py revoke-token --user <你的账号> <id>
```

## 常用流程

**整体跟进审查**（客户跟进是否太空、该排哪些日期）：

```bash
python3 tools/trosa_cli.py snapshot                          # 全部客户 + 全部未完成待办；customers_without_open_task 即没有下一步的客户
python3 tools/trosa_cli.py activity --customer <id> --all    # 钻进某个客户的沟通记录
python3 tools/trosa_cli.py propose-task <id> --title "给 XX 发样品报价跟进" --due 2026-10-08 --reason "上次回复已 12 天"
```

提交后在 Trosa 里确认；Agent 不替成员确认。待办必须同时有**明确动作和日期**；沟通记录只写已经发生的事实，不写推测。

**日常读取**：`today`（今天与逾期）、`customers --query 关键词`、`customer <id>`、`inbox`、`actions`（该令牌最近做过什么）。

## 数据与分页约定

- 所有输出为 JSON；列表接口单页最多 50 条，用 `--offset` 翻页或直接加 `--all`。
- “没有结果”不等于现实中没发生沟通：记录来自 Trosa 已录入的数据（接口返回里带 `fact_policy` 的就是这个提醒）。
- 令牌只对 `/api/gateway/*` 生效。`/api/agent/*`（客户工作区、完整时间线、跨客户消息搜索）要求网页登录会话，PAT 访问会得到 403；需要这些时请先用 `activity` / `customer`，不够再让成员在页面里看。

## 退出码

`0` 成功 · `1` 服务端/参数错误 · `2` 用法错误或没有令牌 · `3` 网络错误 · `4` 令牌无效、已撤销或权限不足。

## 给维护者

接口契约与幂等回执在 `app.py` 的 `gateway_*` 路由；测试在 `tests/test_agent_cli.py`（CLI，离线）与 `tests/test_risk_regressions.py`（`agent_gateway` 相关用例）。新增网关读接口时，同步在 CLI 里加命令并更新本页。
