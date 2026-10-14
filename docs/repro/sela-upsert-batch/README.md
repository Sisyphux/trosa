# Sela 线索写入：单次 upsert 的 SQL 语句数不再随客户数增长

场景：`POST /api/integrations/sela/prospects`（Sela 批量写入）每次都扫全部客户。`_sela_match_customers` 先 `_active_customers`，再对每个客户单独读联系人；`_sela_upsert_contact` 又对每个客户读一遍联系人去重。批量同步因此是 O(N²)，2026-10-14 事故里 324 次写入在边缘被取消而服务端仍在执行。列表接口（s9）不在本任务范围，未改动。

## 结论（隔离 PostgreSQL 演练库，回环地址；不是 ECS 数字）

测量：`measure_upsert.py`，每个规模点 5 次“全新线索”upsert（创建客户路径，匹配的最坏情况），语句数在 `CompatCursor.execute` 层统计。

| 客户数 | 修复前 语句数 / 耗时 | 修复后 语句数 / 耗时 |
|---|---|---|
| ≥100 | 240 / 0.052 s | 40 / 0.046 s |
| ≥1,000 | 2,050 / 0.242 s | 40 / 0.046 s |
| ≥6,000 | 6,040 / 0.655 s | 40 / 0.084 s |

- 语句数：修复后 100、1,000、6,000 三个规模完全相同（40），修复前约 `2×N`。
- 6,000 客户下 0.084 s（最大 0.088 s），满足 ≤ 0.1 s。
- 40 条中包含新建客户本身的写入；走“已存在线索更新”路径是 25 条，走 `REVIEW` 路径是 11 条（同样不随客户数变化）。**未达到“< 30”的目标值**：新建路径的 40 条是创建聚合本身的读写，没有再去压。
- 只把联系人改成一次批量读取（中间版本）语句数变成常数（41），但 6,000 客户仍要 0.35 s，因为每次仍把全部客户与联系人读进 Python；所以最终改为按身份条件在 SQL 里收窄（见下）。该中间版本的原始输出没有保存为文件，数字来自当时的日志。

数据：每个规模都用被测代码树自己通过真实 POST 接口造数（`ub-NNNNN`，约 70% 有联系人）。修复前 100/1,000 在独立新库上测，6,000 在修复后造好的同一库上直接测。

## 行为一致（逐字节）

`parity_corpus.py`：先在一个库里建语料（`setup`），用 `CREATE DATABASE … TEMPLATE` 复制成两个相同快照，旧代码（`81d66e79`）与新代码各跑一份，输出每个探针的匹配决定（`_sela_match_customers` 的返回）与 HTTP 响应体。31 个探针覆盖：精确邮箱（含大小写）、电话（格式不同 / 无加号）、WhatsApp 对电话、同域名、同名无网站（候选）、同名异域名（不复用）、多重命中（REVIEW）、已删除客户的邮箱、额外联系人邮箱 / 电话、两个旧客户共用一个账户（`every_owner` 场景）、重复域名（规范化选择与告警）、`&` / `and`、非 ASCII 邮箱 / 电话 / 公司名、外部 ID 已绑定（同域 / 异域 / 身份冲突）、`%` `_` 字符、重复提交。

- `parity-old.json` / `parity-new.json`：原始输出。**唯一差异是 `revision` 字段**（含数据库时间戳，旧代码自己对同一快照连跑两次也不同；已验证）。
- `parity-old.norm.json` / `parity-new.norm.json`：去掉 `revision` 后，`cmp` 通过。
- 没有包含“人工确认的来源身份事实（confirmed_source）”这一分支的探针，该分支只是改成按 id 读一行，**未被语料覆盖**。

```
cmp parity-old.norm.json parity-new.norm.json   # 无输出 = 相同
```

## 修复内容（`app.py`）

- `_sela_match_candidate_rows`：PostgreSQL 下一次固定三条语句 —— 确认事实、可能携带该身份的联系人、一条按“联系人命中 / 外部 ID / 同域名 / 同名”收窄的客户读取（顺序与原先 `updated_at DESC, id DESC` 相同）。SQL 条件是**超集**：非 ASCII 文本一律放行，之后仍由原来的 Python 逻辑（casefold、`_sela_phone_key`、域名 / 名称规范化）做最终判断，因此匹配规则、REVIEW、幂等回执、响应体都没有动。
- `_sela_upsert_contact`：只读取可能与该联系人重复的联系人（同样的超集条件 + 客户名称），不再读整张联系人表。
- `_sela_canonical_duplicate_match.rank`：联系人数量改为一次批量读取。
- SQLite 开发路径不变。无表结构 / 迁移改动。

## 测试

- `tests/test_sela_upsert_batch.py`（SQLite，2 条）：语句数不随客户数增长；重复邮箱仍匹配现有客户。**这条语句数测试在旧代码上也通过**——SQLite 路径本来就是批量读取，不存在 O(N)；真正能在旧代码失败的是下面的 PostgreSQL 测试。
- `tests/test_postgres_rehearsal.py::test_sela_prospect_upsert_issues_the_same_statements_for_any_customer_count`：旧代码 `76 != 102` 失败，新代码两者均为 40 通过。

## 未验证 / 已知

- 这些是演练库数字，不是 ECS：ECS 的数据形状、2 vCPU 与并发争用未测。预期 ECS 上同样不再 O(N)，但具体秒数需要上线后测。
- 旧代码里若某个客户的 `website` 是 `urlparse` 会抛错的值（如非法 `[`），旧代码每次 upsert 都 500；新代码刻意把这类行仍放进候选以保持同样的报错，但语料里没有该用例。
- 公司名只含非 ASCII 或只含 `and` 的线索，条件放宽为扫描全部客户（结果仍然正确，只是没有提速）。
- 数据库侧仍是一次顺序扫描：`trosa.customer_records` 视图的收窄查询在 6,000 行约 30 ms；客户数再涨一个数量级后需要索引或物化列，这里没有做。

## 复现

```bash
export TROSA_REHEARSAL_PORT=<空闲端口>
.venv/bin/python tools/postgres_rehearsal.py start && .venv/bin/python tools/postgres_rehearsal.py migrate
git archive 81d66e79 | tar -x -C <临时目录>/baseline
TROSA_REHEARSAL_DATABASE_URL=postgresql://<user>@127.0.0.1:$TROSA_REHEARSAL_PORT/trosa_rehearsal \
  .venv/bin/python docs/repro/sela-upsert-batch/measure_upsert.py --tree . --label after --out <输出目录> --sizes 100,1000,6000
# 修复前：--tree <临时目录>/baseline（100,1000 用独立新库；6000 设 UB_SEED_FROM=6000 复用上面的库）
# 一致性：parity_corpus.py setup → CREATE DATABASE … TEMPLATE ×2 → run（旧 / 新）→ cmp
```
