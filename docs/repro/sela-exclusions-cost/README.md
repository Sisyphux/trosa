# Sela 排除清单读取：一次调用从 1,026 条查询降到 10 条

场景：`GET /api/integrations/sela/exclusions`。正式站实测 518 条语句 / 0.8 s / 970 KB；`?limit` 无效。

## 根因
PostgreSQL 快照对每个有联系证据的客户各发一条 `_modern_outreach_rows(customer_id=…)`（含相关子查询），再加整表 `SELECT *`（含 profile/notes 大文本）。语句数 ≈ 客户数 + 常数。响应体本身（每条记录）不是浪费，字节数不变。

## 修改
`app.py::_sela_exclusion_snapshot_records`（PostgreSQL 分支）：客户只取用到的 7 列；每个客户的最近外发日期用一条 `DISTINCT ON` 查询取得（排序与原读取一致）。SQLite 分支本来就是固定条数，未改。

## 结果（隔离演练库，回环地址，2,406 个客户/线索，其中 1,064 条记录进入排除清单）
| | 修改前 (81d66e79) | 修改后 |
|---|---|---|
| 每次调用 SQL 语句数 | 1,026 | 10（13 条记录的小库为 9） |
| 每次调用耗时（中位数，进程内） | 0.435 s | 0.071 s |
| 条件请求 If-None-Match | 304，无正文，0.57 s | 304，无正文，0.07 s |
| 响应字节 | 597,766 | 597,766 |
| 去掉 generated_at 后正文 sha256 | 250734cc…b705 | 相同（`cmp` 通过） |
| `?limit=100` | 同样大小（忽略） | 同样大小（忽略，未改） |

SQLite：`sqlite_parity.py` 新旧两棵树对同一库输出 `cmp` 通过（SQLite 分支未改动）。

## 条件请求（为什么没有用 s9 的“计数+最大时间戳”预指纹）
该接口原本就有 ETag / `X-Trosa-Sync-Version` / 304，值是记录内容哈希，正文 `data_version` 也是它；为保持正文逐字节不变，保留不动。s9 的预指纹对这里不安全：`outreach_messages` 没有 `updated_at`，回复/退信只改 `reply_status`，计数和最大时间戳看不到，会把“已回复”漏成 304。因此 304 仍要先构建快照，但现在只要 10 条固定查询。Sela 现有调用都不带 If-None-Match（全量读取），行为不变。

## 未验证 / 未做
- 要求的 6,000 线索/客户规模没有达到：串行 POST 造数随数据量变慢（O(N²)），停在 2,406；6,000 客户处的语句数靠结构论证（无按客户循环）与 2 / 6 / 1,064 三点恒定得出，未实测。
- 0.15 s 目标是演练库数字（0.071 s），不是 ECS 数字。
- 未做分页（`limit` 仍被忽略）：为不破坏 Sela 全量语义，未新增；若要分页应另做显式 opt-in。
- 未做预指纹式“不构建就 304”。

## 复现
`start/migrate` 演练集群 → `seed_prospects.py`、`enrich_prospects.py`、`enrich_exclusions.py` → `measure_exclusions.py --tree <git archive 81d66e79>` 与 `--tree .`，再 `cmp` 两份 `--body-out`。
