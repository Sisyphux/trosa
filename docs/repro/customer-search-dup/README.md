# 搜索同名公司：不再藏住有沟通记录的那条

## 现象

同一家公司有两条记录时，客户台账搜这家公司的名字只显示一行，而且不说明还有另一条。
正式库里出现过的实例：搜「Garima Impex」，显示的那行写着「尚无沟通记录」，而真正带着
2026-08-20 外联事实的那条记录被藏住了。同时，搜「Garima Impex」还会带出只含「Impex」
的其他公司。

## 根因

1. `_get_customers_postgres` 的多词过滤是「任一词命中」（`any(token in haystack)`, OR），
   而 SQLite 分支是逐词 `AND`。两个后端行为不一致，PostgreSQL 上搜「Garima Impex」会把
   所有含「Impex」的公司都带出来。
2. 搜索列表按公司名折叠成一行（`_deduplicate_customer_search_results`），保留的是排序里
   的第一条。两条同名记录的相关度分数相同，谁在前由底层顺序决定，于是可能正好留下空壳，
   而且页面没有任何提示。

## 修复

- `_customer_search_match_contexts` 增加可选的 `coverage` 累加器：把每个词在本条记录的
  字段或它的沟通、开发邮件、联系人、待办、Inbox 条目里命中的词收集起来。
  `_get_customers_postgres` 用「所有词都命中」判定保留（union-of-evidence），与 SQLite 一致。
- `_deduplicate_customer_search_results` 折叠同名公司时优先留下有记录事实（沟通或回信）的
  那条，并在它上面挂 `same_name_duplicates`；`/api/customers/ledger` 把它作为
  `same_name_hints` 返回，台账行内显示「另有 N 条同名记录」，被折叠的那条自己也有沟通时
  标「（含沟通）」。
- `/api/customers/ledger/rows` 的行键保持不变（仍在 `ROW_KEYS` 契约内）。

## 证据

场景脚本：`tools/repro_customer_search_dup.cjs`（在隔离开发库里造一对同名公司：先建空壳、
后建带客户回信的那条，再建一家没有同名的对照公司）。

- 修复前（`git archive HEAD` 导出的干净副本）：`before/report.json` —— **2/5 通过，退出码 20**。
  服务端日志显示搜索落到 `ledger/rows?ids=1`（空壳那条），页面显示「尚无沟通记录」，
  没有同名提示，无障碍名称里也没有。
- 修复后（同一个脚本、同一台隔离服务）：`after/report.json` —— **5/5 通过，退出码 0**。
  搜索落到 `ledger/rows?ids=2`（有回信的那条），显示「客户回复 · 客户回信：确认样品与报价，
  下周下单。」并提示「另有 1 条同名记录」，无障碍名称同样说明。
- 对照检查：没有同名的公司不出现同名提示。

复现命令：

```bash
.venv/bin/python tools/repro_dev_server.py --app-root . --port 18191 --db-dir <临时目录>/db-after
node tools/repro_customer_search_dup.cjs --url http://127.0.0.1:18191 --label after --out docs/repro/customer-search-dup/after
```

修复前的对比：把 `--app-root` 指向 `git archive HEAD` 导出的临时副本，`--label before`。

覆盖这一改动的测试：

- `tests/test_customer_ledger.py`：同名折叠留下有沟通的一条并给出提示、无同名不提示、
  多词搜索必须全部命中，且行负载键不变。
- `tests/test_risk_regressions.py`：PostgreSQL 路径的多词搜索要求每个词都命中
  （原来会返回只含一个词的公司）。
- `tests/test_postgres_rehearsal.py`：在真实 PostgreSQL 上跑同名折叠与多词搜索。

## 边界

- 多词 `AND` 的变化只影响 PostgreSQL 路径；SQLite 本来就是这个行为。
- 折叠仍然只按公司名。真正同名但不同的公司（例如 Plastic World 加拿大 / 南非）不会被合并，
  只会看到「另有 N 条同名记录」的提示。
- 脚本只用本机回环地址和临时 SQLite 库，不连接正式数据。
