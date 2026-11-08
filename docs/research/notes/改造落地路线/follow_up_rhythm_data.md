# Trosa 历史数据里的真实跟进节奏（给 AI“该跟进谁”校准用）

> 统计日：2026-10-27（数据截至当天）。只用维护者只读通道 `tools/trosa_maint_read.py`（只读角色，单条 SELECT），只取**汇总数字**；没有抄录任何客户名、邮箱、电话、沟通原文。业务员只写名字。
> 来源写法：所有数字都来自 [只读库查询 2026-10-27](tools/trosa_maint_read.py)，具体 SQL 见文末“附：可复现查询”。
> 运行方式：`/Users/luoxin/Desktop/Trosa/.venv/bin/python tools/trosa_maint_read.py sql "<SQL>"`（在仓库根目录执行；隧道偶尔断开，重试即可）。

## 口径（先读这个）

- **数据来源**：沟通记录的底表是 `trosa.timeline_events`（`trosa.follow_up_logs` 只是它的投影视图），按 `trosa.accounts.owner_user_id → identity.users.username` 归到业务员。
- **日期**：用 `occurred_at`（沟通发生日，换算到北京时间取日期）。历史记录的时间都是 00:00（只有日期精度，没有时刻）；`created_at` 在 9-03 迁移时被统一重写，不用。
- **去掉的记录**：被删除的记录（payload `is_deleted`）、Sela 退信（`outreach_bounced`/`外联退信`）、Sela 外联发信（`outreach_email`）、`task_completed`、`周内汇总`。Sela 发出的 2,933 封开发信在单独的外联表里，不在时间线中，本文不计入。
- **“客户-日”**：同一客户同一天多条记录合并为 1 个“联系日”，间隔 = 下一个联系日 − 本联系日（天）。
- **两个时期**：`history` = 2026-09-03 之前（以 Excel/旧表迁入为主），`live` = 9-03 起在 Trosa 里新录的。live 只有 8 周，长间隔被截断，**不能拿 live 的中位数当常态**。
- **已归档**：归档 = 客户 `deleted_at` 非空（回收站）。下面的统计默认排除归档客户。
- **类型归并**：email/邮件/Email→邮件；whatsapp/WhatsApp→WhatsApp；quote/报价跟进等→报价；sample/寄样/样品运费跟进→寄样；meeting/会面安排/客户来访→会议；customer_reply/客户回复→客户回复；其余 follow_up/其他跟进/communication 等→一般跟进。
- **谁在动（方向）**：`direction` 字段存在（inbound/outbound/two_way/unknown），但**只有 2026-07 起的记录才填过**，之前全部是 unknown。customer_reply 一律算“客户”。

## 一、连续两次沟通之间隔多久（按业务员、类型、方向、阶段）

### Takeaway
历史上（迁移前）一个客户两次沟通的典型间隔是 **1.5–3 周（中位数 Amy 21 天、Kelley 14 天、Hamid 7 天）**，但尾巴很长：**四分之一的间隔超过 5–6 周，十分之一超过 3 个多月**。报价、寄样之后跟得最紧（中位数 10–16 天），会议之后最松（中位数约 30 天）。客户来信后的下一次记录最快（中位数 3 天）。“阶段”在库里基本不存在，无法按阶段拆。

### Cited Findings
**按业务员 × 时期（非归档客户，客户-日口径）** — [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| 业务员 | 时期 | 联系日数 | 有下一次的 | p25 | 中位数 | p75 | p90 |
|---|---|---|---|---|---|---|---|
| Amy | history | 1,535 | 1,390 | 10 | 21 | 39 | 105 |
| Amy | live | 68 | 40 | 2 | 4 | 9 | 14 |
| Kelley | history | 1,074 | 935 | 7 | 14 | 39.5 | 96 |
| Kelley | live | 44 | 31 | 1.5 | 3 | 7 | 11 |
| Hamid | history | 796 | 556 | 4 | 7 | 31 | 98 |
| Hamid | live | 101 | 37 | 2 | 4 | 10 | 21 |
| Emma | live | 12 | 7 | 6 | 7 | 9.5 | 17 |

**按“这一次是什么类型” → 到下一次任何沟通（history，全队合计，单位：天）** — [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| 类型 | 次数 | 有下一次的 | p25 | 中位数 | p75 | p90 |
|---|---|---|---|---|---|---|
| 一般跟进 | 1,865 | 1,563 | 7 | 18 | 38 | 99 |
| 报价 | 425 | 405 | 7 | 12 | 30 | 92 |
| 会议 | 396 | 359 | 16 | 30 | 103 | 195 |
| 寄样 | 360 | 350 | 7 | 11 | 24 | 88 |
| 邮件 | 248 | 165 | 3 | 7 | 20 | 83 |
| WhatsApp | 142 | 63 | 3.5 | 8 | 15 | 53 |
| 电话 | 23 | 23 | 7 | 13 | 44 | 200 |
| 客户回复 | 18 | 9 | 1 | 4 | 9 | 31 |

分业务员（history，中位数 / p75，天）：
- Amy：一般跟进 21/35，会议 28/104，报价 16/55，寄样 14/36，邮件 12/45，WhatsApp 7.5/12。
- Kelley：一般跟进 19/53，报价 10/18，寄样 10/18，会议 43/83，邮件 3/5.75，电话 13/44。
- Hamid：一般跟进 7/34，WhatsApp 8/23，邮件 5/8，报价 7/14，寄样 12/74.5，会议 70/103，客户回复 4/9。

**按“谁在动”（2026-07-01 起有方向的记录，非归档，天）** — [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| 方向 | 次数 | 有下一次的 | p25 | 中位数 | p75 | p90 |
|---|---|---|---|---|---|---|
| 客户来信/回复 | 107 | 66 | 1 | 3 | 7 | 17.5 |
| 双向 | 71 | 45 | 2 | 4 | 8 | 13.6 |
| 我方发出 | 336 | 113 | 3 | 5 | 16 | 55 |
| 未标方向 | 505 | 283 | 3 | 6 | 13 | 33 |

- 客户来信那天之后，**只有 55/107（51%）后来出现了我方/双向的记录**；有记录的那一半，中位数 3 天、p75 12.5 天、p90 27 天。
- 我方单方发出（当天没有客户来信）之后，**只有 41/327（13%）后来记到了客户回应**；有回应的中位数 9 天、p75 50 天。

**阶段**：`trosa.customer_states.business_stage` 全库只有 1 个客户填了（“成交”），其余为空；`accounts.account_status`（跟进中/已建联/未建联）和等级 A–D 是旧 Excel 字段，C 级占绝大多数。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)

### Inferences
- 固定的 15/30/60 天规则跟真实节奏不吻合：真实节奏取决于“上一步是什么”——报价/寄样后一般 1–2 周就有下一步，会议后常隔 1 个月以上，客户来信后应在几天内动。
- 中位数之外的长尾说明：大约每 4 次沟通就有 1 次之后沉默 5 周以上，这正是 AI 扫描最该补位的地方。
- 方向信息只有近 4 个月才有，且“我方发出后客户是否回了”大多数没被记下来（13%），所以“等客户回复”这类判断目前**只能靠新录入的数据**，历史数据撑不起。

### Gaps
- 无法按“客户阶段”拆分：阶段字段几乎全空。
- live 期太短（8 周），live 的间隔分布被截断，偏短。

## 二、报价/寄样之后多久有下一次沟通；30/60/90 天内再无任何记录的比例

### Takeaway
报价或寄样之后，**下一次记录往往不快**：全队只有 29% 在 7 天内有下一次记录，59% 在 14 天内，74% 在 30 天内；**四分之一（26%）的报价/寄样 30 天内再无任何记录，17% 60 天内再无，15% 90 天内再无**。这比“随便哪一次沟通之后沉默”的基线（41% / 30% / 22%）好，说明报价、寄样确实被跟得更紧，但仍有一成半的报价/寄样之后**三个月完全没有下文**。业务员差别明显：Kelley 报价/寄样后最紧（14 天内 66%，90 天无下文只有 9%），Amy 最松（14 天内 49%，90 天无下文 21%），Hamid 样本太少（48 次）只能看个大概。

### Cited Findings
统计范围：2026-09-03 之前（history）的“联系日”，非归档客户；“下一次”= 同一客户之后任何一个联系日（不论类型、不论谁发起）。为避免把“还没来得及发生”算成“沉默”，30/60/90 天的比例**只统计距今已满 30/60/90 天的那些次**（分别截止 9-27、8-28、7-29）。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)

**报价/寄样之后到下一次任何记录（天）以及无下文比例**

| 触发 | 业务员 | 次数 | 有下一次 | 中位数 | p75 | 30天内无记录 | 60天内无记录 | 90天内无记录 |
|---|---|---|---|---|---|---|---|---|
| 报价 | 全队 | 425 | 405 | 12 | 30 | 28% | 19% | 17% |
| 报价 | Amy | 191 | 189 | 16 | 55 | 34% | 25% | 24% |
| 报价 | Kelley | 205 | 187 | 10 | 18 | 24% | 14% | 11% |
| 报价 | Hamid | 29 | 29 | 7 | 14 | 24% | 14% | 10% |
| 寄样 | 全队 | 360 | 350 | 11 | 24 | 24% | 14% | 13% |
| 寄样 | Amy | 167 | 162 | 14 | 36 | 31% | 20% | 17% |
| 寄样 | Kelley | 174 | 169 | 10 | 18 | 16% | 7% | 6% |
| 寄样 | Hamid | 19 | 19 | 12 | 74.5 | 37% | 26% | 28% |
| 报价或寄样 | 全队 | 785 | 755 | 11 | 25.5 | 26% | 17% | 15% |
| 报价或寄样 | Amy | 358 | 351 | 14 | 46.5 | 33% | 23% | 21% |
| 报价或寄样 | Kelley | 379 | 356 | 10 | 18 | 20% | 11% | 9% |
| 报价或寄样 | Hamid | 48 | 48 | 7 | 35 | 29% | 19% | 17% |
| 基线：任何一次沟通 | 全队 | 3,405 | 2,881 | 16 | 36 | 41% | 30% | 22% |
| 基线：任何一次沟通 | Amy / Kelley / Hamid | 1,535 / 1,074 / 796 | | 21 / 14 / 7 | 39 / 39.5 / 31 | 38% / 39% / 48% | 26% / 27% / 41% | 21% / 23% / 22% |

**报价/寄样之后多久内出现下一次记录（累计占比，全部 785 次）** — [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| 业务员 | 7 天内 | 14 天内 | 30 天内 |
|---|---|---|---|
| 全队 | 29% | 59% | 74% |
| Amy | 22% | 49% | 67% |
| Kelley | 32% | 66% | 80% |
| Hamid | 56% | 71% | 71% |

### Inferences
- 报价/寄样后的“该催了”信号，**第 2 周比第 1 周合适**：第 1 周内本来就只有约三成会有下一次记录，所以“报价满 7 天就提醒”会对大多数人误报；满 14 天时仍无记录的有 41%，这时提醒更像是真漏了。
- 17% 的报价/寄样三个月没有任何后续，这是 AI 每天扫描要找回的主要对象；但其中有一部分是“客户已经下单或明确拒绝，只是没记录”（见第四节、第五节），不能全当漏跟。
- 业务员的节奏本来就不同（Kelley 紧、Amy 松），**阈值应按人校准，而不是全队一个数**。
- 注意“无记录”≠“没跟”：同事的录入习惯是周会前补录（见 trosa_current_state.md 第五节），中间有时只是还没补。

### Gaps
- 这批数据几乎全是 9-03 前 Excel 迁入的“历史”，live 期（9-03 起仅 8 周）报价/寄样记录很少（近 30 天同事几乎不再标“报价/寄样”类型），不足以单独成表。
- 只统计“有一条标成报价/寄样类型的记录”；报价其实写在“一般跟进”文字里的没有算进来（见第三节的关键词补充）。
- Hamid 的报价/寄样只有 48 次，比例不稳。
- 没有区分“下一次记录是客户回复还是我方再催”（历史记录无方向）。

## 三、现在每种“该跟进”情形各有多少客户，以及每人每天大约会冒出几条建议

### Takeaway
把四类信号都按“今天（2026-10-27）”的真实库存数一遍：**合计只有约 79 个客户会被点名**（Hamid 35、Kelley 23、Amy 21、Emma 0），不是几百个。对比一下：如果还用“最后一次联系超过 30 天就提醒”的老办法，会点名 **549 个**（31–90 天 265 个、91 天以上 284 个）。稳态下每人每个工作日大约只会产生 **0.5–3 条**建议，所以今天页**不会被淹没**，更大的风险是：**Emma 几乎一条都没有**（只有 2 个客户、12 条记录），Amy、Kelley 若只靠严格信号也偏少。另一个必须先解决的问题：报价/寄样这类“类型标签”在 2026-07 之后几乎不再被录入（10 月是 0 条），客户方向也只有一半记录有，所以信号要靠**正文关键词 + 方向**来补，不能只靠类型字段。

### Cited Findings
统计口径：非归档客户；“年龄”= 今天（2026-10-27）减去该客户**最后一个联系日**；只看这最后一天的内容。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)

**甲、客户最后发言、我方还没回（方向可判断时）**
- 判断方法：该客户最后一个联系日里有“客户”记录（方向=inbound，或类型=客户回复），且同一天没有“我方发出/双向”记录；因此之后任何我方记录都会让它消失。方向字段只有 2026-07 起才有：最近 90 天有记录的 350 个客户里，最后一天方向已知的有 271 个（77%）；分人看 Hamid 219/248、Amy 38/75、Kelley 12/22、Emma 2/5。
- 现在这种客户一共 41 个（Hamid 38、Kelley 2、Amy 1）；按等了多久：

| 业务员 | 等了 >1 天（2–30天） | >2 天（3–30天） | >5 天（6–30天） | 超过 30 天（已冷） |
|---|---|---|---|---|
| Hamid | 12 | 12 | 9 | 22 |
| Amy | 0 | 0 | 0 | 1 |
| Kelley | 0 | 0 | 0 | 2 |
| Emma | 0 | 0 | 0 | 0 |
| 合计 | 12 | 12 | 9 | 23 |

  其中 11/12 的客户来信来自 Sela 回信引擎。反过来，“我方最后发言、等客户回”（30 天内）共 28 个（Hamid 20、Amy 6、Emma 1、Kelley 1）。
- 补充：2026-07-01 至 10-20 这 16 周里，“客户来信、同日我方无记录”的联系日共 86 个（Hamid 66＝每周约 4.1 个，Amy 12＝0.8，Kelley 8＝0.5）。其中 **76 个（88%）在 1 天内没有我方的任何后续记录，69 个（80%）2 天内没有，57 个（66%）5 天内没有，37 个（43%，几乎都是 Hamid）至今没有任何后续记录**。

**乙、报价/寄样之后没有任何记录**
- 只看“类型=报价/寄样”：最后一条记录就是报价/寄样的客户全库只有 32 个（Amy 7、Hamid 2、Kelley 23），**全部已超过 30 天**，没有一个在 8–30 天内。原因：报价/寄样这类标签在 2026-07 后基本没人再标（按月看含报价/寄样的联系日：2026-01 41、04 54、05 30、06 25、07 13、08 1、09 4、10 0）。
- 扩展为“类型是报价/寄样，或正文含 报价/quotation/quote/样品/寄样/sample/PI/proforma”：

| 业务员 | 8–14 天 | 15–30 天 | 31–90 天 | 91–180 天 | >180 天 | 合计 >7 天 | >14 天 | >30 天 |
|---|---|---|---|---|---|---|---|---|
| Hamid | 6 | 2 | 11 | 7 | 0 | 26 | 20 | 18 |
| Kelley | 1 | 0 | 7 | 14 | 9 | 31 | 30 | 30 |
| Amy | 0 | 0 | 11 | 3 | 5 | 19 | 19 | 19 |
| Emma | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 合计 | 7 | 2 | 29 | 24 | 14 | 76 | 69 | 67 |

  真正“还新鲜”（8–90 天）的是 38 个（Hamid 19、Amy 11、Kelley 8），超过 90 天的 38 个大概率已经冷掉或早已成交。

**丙、成交过的老客户 60/90/180 天没联系**
- 库里**没有可靠的“成交”标记**：`business_stage`＝“成交”全库只有 1 个客户；订单类事件类型（订单执行/首单准备/订单跟进）全库只有 3 条。因此用两个替代口径：
  1. **“正文出现下单/订单/PO/PI/定金/尾款/付款/出货/发货/订舱/复购等词，且不含‘没有订单/未下单’等否定”**（上限估计，可能含误判）：62 个客户（Amy 25、Kelley 22、Hamid 15、Emma 0）。最后联系已超过 60 天的 34 个（Kelley 19、Amy 12、Hamid 3），超过 90 天的 29 个（18/10/1），超过 180 天的 9 个（Kelley 7、Amy 2）；60–365 天内的 29 个。
  2. **Excel 导入时的“客户类型=existing（老客户）”标记**（有记录的 458 个：Hamid 240、Amy 104、Kelley 111、Emma 3）：最后联系 >60 天的 374 个（Hamid 188、Kelley 103、Amy 83），>90 天 191 个（Kelley 94、Hamid 56、Amy 41），>180 天 57 个（Kelley 55）。这个标记很宽——Hamid 名下有 1,181 个“existing”，其中大部分从没有过沟通记录，不能当作“已成交”。

**丁、正文里有未来时间说法（下周/月底/节后/next week/after Ramadan/“N 周后”等关键词计数）**
- 最近 30 天 147 条记录里只有 **5 条（3.4%）**带这类说法（Hamid 3/75、Amy 2/38、Kelley 0/28、Emma 0/6），涉及 5 个客户；其中“下周/next week”4 条、节日类 1 条、“月底/月中/N 周后”0 条。2026-04-27 至 09-02 的 1,264 条历史记录里是 28 条（2.2%）。
- 这 5 个里，“这条就是该客户最后一条记录且已过 7 天”的有 2 个（均为 Hamid）；也就是说“说了下周，到期没动”的目前只有 2 个。

**四类合并后的库存（去重）** — [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| 业务员 | 甲 客户在等（2–30天） | 乙 报价/寄样（8–90天，含关键词） | 丙 成交过且 61–365 天未联系（口径1） | 丁 有未来说法且 3–45 天无后续 | 去重合计 |
|---|---|---|---|---|---|
| Hamid | 12 | 19 | 3 | 2 | 35 |
| Kelley | 0 | 8 | 15 | 0 | 23 |
| Amy | 0 | 11 | 11 | 0 | 21 |
| Emma | 0 | 0 | 0 | 0 | 0 |
| 合计 | 12 | 38 | 29 | 2 | 79 |

对照：全库非归档、有过记录的 634 个客户里，最后联系日在 8–14 天前的 39 个、15–30 天前的 14 个、31–90 天前的 265 个（Hamid 197）、91 天以上的 284 个。

**每人每个工作日会新冒出多少条（稳态粗估，按 22 个工作日/月）**

| 业务员 | 甲 客户来信没人回 | 乙 报价/寄样后沉默>14天 | 丙 成交老客户到点 | 丁 未来说法到期 | 合计 |
|---|---|---|---|---|---|
| Hamid | 约 0.8（每周 4.1 个来信日，近九成 1 天内无后续记录） | 0.04 | 0.1–1.6 | <0.1 | 约 1–3 |
| Amy | 约 0.16 | 0.37 | 0.1–0.7 | <0.1 | 约 0.7–1.3 |
| Kelley | 约 0.1 | 0.22 | 0.1–0.9 | 0 | 约 0.4–1.2 |
| Emma | 约 0 | 约 0 | 约 0 | 约 0 | 约 0 |

估算方法：甲＝2026-07 至 10 月客户来信日的周均数 ÷5；乙＝2025-01 至 2026-06 月均报价/寄样联系日 ×“之后 14 天内无任何记录”的比例（Amy 69%、Kelley 36%、Hamid 30%，全队 49%）÷22；丙＝上面两个口径的库存数 ÷120 天（假设每个老客户约每 4 个月被点名一次，范围下限为口径1、上限为口径2）；丁＝近 30 天 5 条 ÷22。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)

### Inferences
- **不会淹没，但第一天要限流**：上线首日的库存约 79 个（口径1），按每人每天最多 3–5 条放出，一周内消化完；若误用“existing”宽标记当成交，首日库存会多出 370 多个，才会淹没，应避免。
- **Hamid 的“客户在等”是最大来源，也是误报最多的**：他每周 4 个客户来信日，其中 43% 此后在 Trosa 里一直没有记录，但很多是 Sela 回信引擎自动落的记录、真实回复可能走邮件而没回填；所以“客户最后发言”对 Hamid 应当“提示”而不是“判定我方没回”，对 Amy、Kelley 则几乎不会触发（每周 <1 次）。
- **Amy、Kelley 的今天页靠“报价/寄样后沉默”和“老客户到点”撑着**；Emma 在拿到足够记录之前，今天页必然是空的，需要另想办法（例如引导她导入/补录客户）。
- “未来说法”只有 3% 的记录带，当作辅助加分项可以，不能当主信号。
- 类型字段已经不可信（7 月起没人标报价/寄样），自动扫描必须读正文关键词，或让 AI 读最近几条记录判断阶段。

### Gaps
- 方向字段仅 2026-07 起可用，Amy、Kelley 最近 90 天有记录的客户里只有约一半最后一天方向已知，“客户在等”对他们是低估。
- “成交”口径靠关键词，没有订单对象；关键词会把“订单”一词的各种提及（询问、对方拒绝、他人订单）算进去，故为上限；也会漏掉用其他词（如具体产品数量、柜号）描述的订单。
- 邮件/WhatsApp 的真实往来没有进 Trosa（见 trosa_current_state.md 第四节），“最后发言”只代表“记录里的最后发言”。
- 稳态估算中“丙”的区间很宽（±一个量级），因为取决于要不要对老客户设固定回访周期，并非数据决定。
- 同一天内的先后顺序判断不了（时间都是 00:00）：同日既有客户来信又有我方记录时一律算“已回”。

## 四、成交客户与变冷客户，在“下单”前几周的节奏对比

### Takeaway
**能识别，但只能很粗**：库里没有订单对象，也几乎没人填“成交”阶段，只能用“正文里出现下单/订单/PO/定金/付款/出货等词”来圈出 **62 个“疑似成交”客户**，拿它们和 **255 个“变冷客户”**（没有成交信号、最后一次联系已超过 90 天）比。差别很清楚：成交前 8 周里，成交客户平均每人有约 **3.8 个联系日**，变冷客户只有 **1.1 个**；成交前 4 周内有过联系的占 **87%**，变冷的只有 **32%**；成交前最后一次间隔的中位数 **14 天**，变冷前最后一次间隔 **55 天**。**变冷其实在“最后一次联系”之前就能看到：最后两次联系之间的间隔拉长到平时的两倍以上，变冷客户占 50%，成交客户只占 17%。** 但这些数字有明显的“幸存者”偏差（见 Gaps），只能当方向，不能当阈值。

### Cited Findings
- **怎么识别“成交”**：①`business_stage`＝“成交”的全库只有 1 个客户；②订单类事件类型（订单执行/首单准备/订单跟进）全库 3 条；③周报“订单签约”标签只存在于界面，没有落成可查字段；④因此改用正文关键词（含“下单、订单、PO、PI、定金、尾款、付款、到账、出货、发货、订舱、复购”等，并排除“没有订单/未下单”等否定），把**首次出现这类词的联系日**当作“下单日”（锚点）。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)
- **“变冷客户”**：没有成交信号、最后联系日距今 >90 天；以**最后一次联系日**为锚点。

**锚点之前的节奏（不含锚点当天；仅统计锚点前至少有 2 个联系日的客户：成交 39 个，变冷 165 个）** — [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| 指标 | 成交（疑似） | 变冷 |
|---|---|---|
| 客户数（全部 / 前面≥2个联系日） | 62 / 39 | 255 / 165 |
| 锚点前累计联系日中位数 | 7 | 5 |
| 锚点前 8 周联系日：中位数 / 平均 | 3 / 3.8 | 1 / 1.1 |
| 锚点前 4 周内有联系的占比 | 87% | 32% |
| 锚点前 8 周内联系 ≥2 天的占比 | 85% | 26% |
| 锚点前最后一次联系距锚点（中位数，天） | 14 | 55 |
| 锚点前 90 天内出现过报价的占比 | 44% | 16% |
| 锚点前 90 天内出现过寄样的占比 | 28% | 6% |

分业务员（同样口径）：

| 业务员 | 群 | 客户数 | 8 周联系日平均 | 4 周内有联系 | 最后一次距锚点中位数（天） | 前 90 天有报价 | 前 90 天有寄样 |
|---|---|---|---|---|---|---|---|
| Amy | 成交 | 25（13 个够算） | 3.4 | 92% | 14 | 62% | 38% |
| Amy | 变冷 | 88（81） | 0.9 | 27% | 55 | 9% | 7% |
| Kelley | 成交 | 22（17） | 3.2 | 88% | 14 | 35% | 29% |
| Kelley | 变冷 | 112（50） | 0.8 | 32% | 65 | 34% | 6% |
| Hamid | 成交 | 15（9） | 5.4 | 78% | 5 | 33% | 11% |
| Hamid | 变冷 | 55（34） | 2.2 | 41% | 30.5 | 9% | 3% |

**变冷的“前兆”：最后一个间隔相对平时是否明显拉长（锚点前至少 3 个联系日：成交 35 个，变冷 123 个）** — [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| 指标 | 成交（疑似） | 变冷 |
|---|---|---|
| 平时间隔中位数（天） | 8 | 19.5 |
| 锚点前最后一个间隔中位数（天） | 14 | 37 |
| 最后间隔 >平时间隔 2 倍（最少按 7 天算）的占比 | 17% | 50% |
| 最后间隔 >30 天的占比 | 14% | 54% |

### Inferences
- 成交前的典型画面是：**最近 4 周内几乎都在密集联系，8 周内有 3 次以上，之前常有报价或寄样**。变冷前的典型画面是：联系本来就稀（平时约 3 周一次），最后一个间隔越拉越长。
- 对“该跟谁”的启发：与其用固定天数，不如拿**这个客户自己的平时间隔**做尺子——超过平时间隔约 2 倍还没动静，就值得提醒。变冷客户里有一半在最后一次联系前就已经满足这一条，说明这个信号有区分度。
- 报价、寄样出现过的客户更容易成交（44% / 28% 对 16% / 6%），所以“报价/寄样后的沉默”优先级应高于普通沉默。
- 只能说明相关，不能说明因果（见下）。

### Gaps
- **成交识别很粗**：靠关键词，既会把“聊到订单但没成”的也圈进去（偏多），也会漏掉没有写这些词的真实成交（它们大概率落在“变冷”那一群里，让对比被稀释）。
- **幸存者偏差**：成交客户之所以出现订单字样，本身是因为沟通密集、记得多；变冷客户的最后一条记录之前，很多本来就只是 Excel 里的单条旧记录（255 个里只有 165 个前面有 ≥2 个联系日）。所以两群的“联系日多少”差异被夸大。
- 两群的锚点含义不同（成交＝首次出现订单词；变冷＝最后一次联系），且一部分“变冷”客户其实是正常的长周期老客户（半年一单）。
- 成交客户只有 62 个，分人后每格只有十几个，Hamid 尤其少。
- 历史日期大多来自 Excel，只有日期精度；“前 4 周”“前 8 周”的边界有几天误差。

## 五、会误导自动扫描的数据质量问题（带规模估计）

### Takeaway
最大的坑有四个：**①73% 的客户（1,752/2,386）在时间线里一条记录都没有**，其中绝大多数是 Hamid 名下 Sela 开发的潜在客户（1,675 个）；**②所有记录都只有日期、没有时刻，且约 19% 的记录落在“批量补日期”的几天里**，同一天先后、以及这几天前后的间隔都不可信；**③86% 的记录没有方向、56% 的记录是“一般跟进”**，类型和方向信息稀薄；**④`last_contact_at`、`contact_state`、`next_task_date` 一类汇总字段不可信**（1,535 个有 last_contact_at 的客户里 929 个根本没有对应记录），扫描必须从时间线重算。重复客户、退信、已归档客户的影响反而较小。

### Cited Findings
统计范围：除特别说明外为非归档客户、已去掉删除/退信/外联发信/待办完成/周内汇总的时间线记录（共 3,917 条、634 个有记录的客户）。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)

| # | 问题 | 规模 | 会怎么误导扫描 |
|---|---|---|---|
| 1 | **客户在时间线里没有任何记录** | 非归档客户 2,386，有记录的 634，**无记录 1,752（73%）**：Hamid 1,675（名下 1,979，其中“未建联”586）、Amy 75（名下 248）、Kelley 2、Emma 0。有记录的 634 个里，**只有 1 个联系日的 191 个**，≥2 个联系日的 443 个。 | 无记录的客户既不能算“很久没联系”，也不能算“从没联系”；有一个联系日的客户算不出节奏，只能用全队中位数。 |
| 2 | **记录只有日期没有时刻** | 3,990 条里 3,912 条（98%）时间是 00:00；2026-07 之后新录的也有 1,019/1,097（93%）。 | 同一天内“谁先谁后”判断不了；“客户最后发言”“已回复”按同日一律算已回。 |
| 3 | **批量补日期的日子** | 单个业务员一天 ≥25 条的日子有 18 个，共 728 条（占 19%）：Amy 10 天/405 条、Hamid 6 天/244 条、Kelley 2 天/79 条。全队最集中的日期：2026-08-03（84）、01-14（74）、07-17（69）、08-04（59）、03-02（58）、07-30（46）。 | 这些日子前后的“间隔”是人为的（要么 0，要么被拉长），把一次性 Excel/补录当成当天真实联系；“最近联系”会虚高。 |
| 4 | **录入延迟 / 周一补录** | 历史记录里 26% 落在周一（均匀应约 20%），5% 落在周末；有 115 条日期被人工更正过（年份修正）。另见 trosa_current_state.md：Amy 47%、Kelley 38%、Hamid 28% 的手工记录晚录入 ≥2 天。 | 记录日期比真实沟通日晚，“X 天没联系”会偏大几天。 |
| 5 | **方向和类型稀薄** | 无方向 3,385/3,917（86%）；类型是“一般跟进” 2,195/3,917（56%）；报价/寄样类型标签 2026-07 后基本消失（见第三节）。 | “谁在等谁”只能用于最近 4 个月；类型信号不可靠，需要读正文。 |
| 6 | **汇总字段不可信** | `accounts.last_contact_at` 有值 1,535 个，其中 **929 个在时间线里没有任何记录**；有记录的里 120 个与时间线最后一天相差 >7 天；另有 28 个有记录但该字段为空。（`contact_state`、`next_task_date` 的不可信见既有结论。） | 不能用这些字段直接判断“多久没联系”。 |
| 7 | **Sela 自动记录与外联混在时间线里** | 退信记录涉及 44 个客户，其中 **28 个客户除退信外没有任何真实记录**，另有 12 个客户“最新一条”是退信；Sela 回信引擎写入的非退信记录 25 条；客户来信类记录共 118 条，其中正文含自动回复/休假类字样的 5 条。Sela 外联发信 2,942 条（涉及 1,774 个客户）在单独的外联表，不在时间线。 | 若不排除退信，会把退信当成“最近联系”；若只看时间线，1,774 个收过开发信的客户会被当成“从没联系”；自动回复会被当成客户“发言”。 |
| 8 | **重复客户** | 按名称（去掉标点、统一大小写）重名的组 340 个，涉及 686 个客户（占 29%）；其中跨业务员重名 84 组/169 个客户；**两个以上重名客户都有记录的只有 11 组**（时间线被拆开）。邮箱重复仅 1 组（2 个客户）。名称重名含通用名，是上限。 | 同一客户被拆成两份：一份显示“很久没联系”，另一份有近期记录，会出现误报；跨业务员的还可能两个人都被提醒。规模较小，主要集中在 Hamid 与 Sela。 |
| 9 | **同条记录重复** | 同一客户同一天正文完全相同的重复记录多出 187 条（占 4.8%）；同一客户同日 ≥5 条的有 3 个客户日、共 17 条。 | 会夸大“联系频率”，不影响“最后一次联系”。 |
| 10 | **已归档客户** | 归档客户 66 个，其中 13 个有 73 条记录，**4 个在近 90 天内还有记录**。 | 必须按 `deleted_at` 过滤，否则会提醒已移入回收站的客户；4 个“归档后仍有联系”的需要人工判断是否该恢复。 |
| 11 | **遗留的自动开发节点** | 约 1,190 条未完成的 15/30/60 天旧提醒（Hamid 583、Amy 306、Kelley 299 等，见 trosa_current_state.md）。 | 如果扫描读了待办表，会被这批假逾期淹没；扫描应只读时间线，不读这些旧行。 |
| 12 | **“老客户”标记太宽** | `customer_type='existing'` 共 1,475 个，其中 Hamid 1,181 个，有记录的只有 240 个。 | 不能当“成交客户”用（见第三节）。 |

### Inferences
- 对扫描最重要的三条过滤规则：**只读时间线、排除退信/外联/已归档、有记录才算**；这样 2,386 个客户里真正进入判断的只有约 634 个（Hamid 304、Amy 173、Kelley 152、Emma 5）。
- 对 Hamid 的 1,675 个“无记录”客户，扫描应视为“Sela 在处理的潜在客户”，只有在出现客户回信后才进入 Trosa 的“该跟进”视野；否则每天会被几百个“从没联系”淹没。
- 间隔类的结论（第一、二节）受第 3、4 条影响，中位数多半仍成立，但对“几天内”的精确阈值要留余量（建议以周为单位而不是天）。
- 用 AI 读正文判断“有没有回复/有没有报价/有没有下单”，比依赖类型、方向、汇总字段更稳。

### Gaps
- 重复客户只按名称、邮箱粗查，没按网站/电话/联系人匹配，实际重复可能更多，也可能含通用名误报。
- 没有统计联系人级数据（联系人兼容视图口径不可靠，见 trosa_current_state.md），也没统计一个客户有多位联系人时的错配。
- 外联表与客户的对应因 ID 类型不同（旧编号）未做精确关联，只能给整体规模；“收过开发信但无时间线记录”的精确人数没算。
- 没有核对自动回复的真实比例：只用 5 个关键词粗查，且排除了正文为空的记录。
- 批量补日期的阈值（单日 ≥25 条）是经验值。

## 附：可复现查询

公共前缀（下面每条查询都以它开头，`<BASE>` 指这一段）：

```sql
WITH ev AS (
  SELECT u.username AS owner, e.account_id, (a.deleted_at IS NOT NULL) AS archived,
         (e.occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
         e.source_module AS src, e.direction,
         CASE
           WHEN lower(e.event_type) IN ('email','邮件') THEN 'email'
           WHEN lower(e.event_type) = 'whatsapp' THEN 'whatsapp'
           WHEN e.event_type IN ('quote','报价跟进','报价与样品推进','下一单价格跟进') THEN 'quote'
           WHEN e.event_type IN ('sample','寄样','样品运费跟进') THEN 'sample'
           WHEN e.event_type IN ('meeting','会面安排','客户来访','项目与会面跟进') THEN 'meeting'
           WHEN e.event_type IN ('phone','电话') THEN 'phone'
           WHEN e.event_type IN ('customer_reply','客户回复') THEN 'customer_reply'
           WHEN e.event_type IN ('微信','领英') THEN 'wechat_linkedin'
           WHEN e.event_type IN ('订单执行','首单准备','首单推进','订单跟进') THEN 'order'
           ELSE 'follow_up' END AS typ,
         CASE WHEN e.event_type IN ('customer_reply','客户回复') OR e.direction='inbound' THEN 'customer'
              WHEN e.direction='outbound' THEN 'us'
              WHEN e.direction='two_way' THEN 'two_way' ELSE 'unknown' END AS who,
         e.content, e.result, e.next_plan
  FROM trosa.timeline_events e
  JOIN trosa.accounts a ON a.id = e.account_id
  JOIN identity.users u ON u.id = a.owner_user_id
  WHERE lower(coalesce(e.payload->>'is_deleted','0')) NOT IN ('1','true')
    AND e.event_type NOT IN ('outreach_bounced','外联退信','outreach_email','task_completed','周内汇总')
),
cd AS (
  SELECT owner, account_id, archived, day,
         bool_or(who='customer') has_cust, bool_or(who='us') has_us,
         bool_or(typ='quote') has_quote, bool_or(typ='sample') has_sample,
         string_agg(DISTINCT typ, ',') typs
  FROM ev GROUP BY 1,2,3,4
),
gaps AS (
  SELECT cd.*, LEAD(day) OVER (PARTITION BY account_id ORDER BY day) AS next_day,
         LAG(day) OVER (PARTITION BY account_id ORDER BY day) AS prev_day,
         CASE WHEN day < DATE '2026-09-03' THEN 'history' ELSE 'live' END AS era
  FROM cd
)
```

Q1 按业务员 × 时期：
```sql
<BASE>
SELECT owner, era, count(*) contact_days, count(next_day) with_next,
 percentile_cont(0.25) WITHIN GROUP (ORDER BY next_day-day) p25,
 percentile_cont(0.5)  WITHIN GROUP (ORDER BY next_day-day) p50,
 percentile_cont(0.75) WITHIN GROUP (ORDER BY next_day-day) p75,
 percentile_cont(0.9)  WITHIN GROUP (ORDER BY next_day-day) p90
FROM gaps WHERE NOT archived GROUP BY 1,2 ORDER BY 1,2
```

Q2 按类型（history）：
```sql
<BASE>
, t AS (SELECT DISTINCT ev.owner, ev.account_id, ev.day, ev.typ, g.next_day, g.era
        FROM ev JOIN gaps g USING (account_id, day) WHERE NOT ev.archived)
SELECT coalesce(owner,'ALL') owner, typ, count(*) n, count(next_day) with_next,
 percentile_cont(0.25) WITHIN GROUP (ORDER BY next_day-day) p25,
 percentile_cont(0.5)  WITHIN GROUP (ORDER BY next_day-day) p50,
 percentile_cont(0.75) WITHIN GROUP (ORDER BY next_day-day) p75,
 percentile_cont(0.9)  WITHIN GROUP (ORDER BY next_day-day) p90
FROM t WHERE era='history' GROUP BY GROUPING SETS ((typ),(owner,typ)) HAVING count(*)>=10 ORDER BY 1,3 DESC
```

Q3 按方向（2026-07 起）：同 Q2，把 `typ` 换成 `who`，条件换成 `ev.day >= DATE '2026-07-01'`。
来信后我方是否跟上：
```sql
<BASE>
, d AS (SELECT * FROM gaps WHERE NOT archived AND day >= DATE '2026-07-01')
SELECT 'cust_in -> next us/two_way' k, count(*) n, count(nx) with_next,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY nx-day) p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY nx-day) p75,
  percentile_cont(0.9) WITHIN GROUP (ORDER BY nx-day) p90
FROM (SELECT d.*, (SELECT min(e.day) FROM ev e WHERE e.account_id=d.account_id AND e.day>=d.day
                   AND e.who IN ('us','two_way')) nx FROM d WHERE has_cust) x
UNION ALL
SELECT 'us_out(only) -> next cust/two_way', count(*), count(nx),
  percentile_cont(0.5) WITHIN GROUP (ORDER BY nx-day), percentile_cont(0.75) WITHIN GROUP (ORDER BY nx-day),
  percentile_cont(0.9) WITHIN GROUP (ORDER BY nx-day)
FROM (SELECT d.*, (SELECT min(e.day) FROM ev e WHERE e.account_id=d.account_id AND e.day>d.day
                   AND e.who IN ('customer','two_way')) nx FROM d WHERE has_us AND NOT has_cust) y
```

### 第二节用到的查询

Q4 报价/寄样之后的下一次与 30/60/90 天无下文比例（只统计距今已满 N 天的次数；history 时期，非归档）：
```sql
<BASE>
, g2 AS (SELECT owner, account_id, day, next_day, era, has_quote, has_sample FROM gaps WHERE NOT archived),
t AS (
  SELECT 'quote' trig, * FROM g2 WHERE has_quote
  UNION ALL SELECT 'sample', * FROM g2 WHERE has_sample
  UNION ALL SELECT 'quote_or_sample', * FROM g2 WHERE has_quote OR has_sample
  UNION ALL SELECT 'any_contact(baseline)', * FROM g2
)
SELECT coalesce(owner,'ALL') owner, trig, count(*) n, count(next_day) with_next,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY next_day-day) p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY next_day-day) p75,
  round(100.0*count(*) FILTER (WHERE day <= DATE '2026-09-27' AND (next_day IS NULL OR next_day-day>30))/nullif(count(*) FILTER (WHERE day <= DATE '2026-09-27'),0),0) pct_none30,
  round(100.0*count(*) FILTER (WHERE day <= DATE '2026-08-28' AND (next_day IS NULL OR next_day-day>60))/nullif(count(*) FILTER (WHERE day <= DATE '2026-08-28'),0),0) pct_none60,
  round(100.0*count(*) FILTER (WHERE day <= DATE '2026-07-29' AND (next_day IS NULL OR next_day-day>90))/nullif(count(*) FILTER (WHERE day <= DATE '2026-07-29'),0),0) pct_none90
FROM t WHERE era='history'
GROUP BY GROUPING SETS ((trig),(owner,trig)) ORDER BY 2,1
```

Q5 报价/寄样后 7/14/30 天内出现下一次的累计占比：
```sql
<BASE>
, g2 AS (SELECT owner, day, next_day, era, has_quote, has_sample FROM gaps WHERE NOT archived AND era='history' AND day <= DATE '2026-09-27')
SELECT coalesce(owner,'ALL') owner, count(*) n,
 round(100.0*count(*) FILTER (WHERE next_day-day<=7)/count(*)) within7,
 round(100.0*count(*) FILTER (WHERE next_day-day<=14)/count(*)) within14,
 round(100.0*count(*) FILTER (WHERE next_day-day<=30)/count(*)) within30
FROM g2 WHERE has_quote OR has_sample GROUP BY ROLLUP(owner) ORDER BY 1
```

### 第三节用到的查询

“最后联系日”快照的公共写法：`DATE '2026-10-27' - day` 为年龄，只看每个非归档客户最后一个联系日。`<TXT>` 指 `lower(coalesce(content,'')||' '||coalesce(result,'')||' '||coalesce(next_plan,''))`。

Q6 甲：客户最后发言且我方没回（含方向已知率）：
```sql
<BASE>
, ld AS (
  SELECT account_id, owner, day,
    bool_or(who='customer') c, bool_or(who IN ('us')) u, bool_or(who='two_way') tw,
    bool_or(who='customer' AND src='sela_reply_engine') c_sela,
    bool_or(who<>'unknown') known
  FROM ev e WHERE NOT archived AND day = (SELECT max(day) FROM ev e2 WHERE e2.account_id=e.account_id AND NOT e2.archived)
  GROUP BY 1,2,3
)
SELECT owner, count(*) accts_with_records,
 count(*) FILTER (WHERE DATE '2026-10-27'-day <= 90) active90,
 count(*) FILTER (WHERE DATE '2026-10-27'-day <= 90 AND known) active90_dir_known,
 count(*) FILTER (WHERE c AND NOT u AND NOT tw) cust_last_all,
 count(*) FILTER (WHERE c AND NOT u AND NOT tw AND DATE '2026-10-27'-day BETWEEN 2 AND 30) gt1_le30,
 count(*) FILTER (WHERE c AND NOT u AND NOT tw AND DATE '2026-10-27'-day BETWEEN 3 AND 30) gt2_le30,
 count(*) FILTER (WHERE c AND NOT u AND NOT tw AND DATE '2026-10-27'-day BETWEEN 6 AND 30) gt5_le30,
 count(*) FILTER (WHERE c AND NOT u AND NOT tw AND DATE '2026-10-27'-day > 30) gt30,
 count(*) FILTER (WHERE c AND NOT u AND NOT tw AND c_sela AND DATE '2026-10-27'-day BETWEEN 2 AND 30) gt1_le30_sela,
 count(*) FILTER (WHERE u AND NOT c AND DATE '2026-10-27'-day <= 30) us_last_le30
FROM ld GROUP BY ROLLUP(owner) ORDER BY 1
```

Q7 甲（流量）：2026-07-01 至 10-20 客户来信日之后我方多久有记录（方向已标 / 任何类型两个版本）：
```sql
<BASE>
, c AS (SELECT cd.owner, cd.account_id, cd.day,
   (SELECT min(e.day) FROM ev e WHERE e.account_id=cd.account_id AND e.day>cd.day AND e.typ<>'customer_reply' AND e.who<>'customer') reply_day
  FROM cd WHERE NOT archived AND has_cust AND NOT has_us AND day BETWEEN DATE '2026-07-01' AND DATE '2026-10-20')
SELECT coalesce(owner,'ALL') owner, count(*) cust_days,
 count(*) FILTER (WHERE reply_day IS NULL OR reply_day-day>1) any_gt1,
 count(*) FILTER (WHERE reply_day IS NULL OR reply_day-day>2) any_gt2,
 count(*) FILTER (WHERE reply_day IS NULL OR reply_day-day>5) any_gt5,
 count(*) FILTER (WHERE reply_day IS NULL) never
FROM c GROUP BY ROLLUP(owner) ORDER BY 1
```
（带方向版本把子查询条件换成 `e.day>=cd.day AND e.who IN ('us','two_way')`，结果 77/72/68，与上面接近。）

Q8 乙：类型=报价/寄样的“最后一条”分布（把 `has_quote OR has_sample` 换成下面的关键词版本即得扩展口径）：
```sql
<BASE>
, ld AS (SELECT DISTINCT ON (account_id) account_id, owner, day, has_quote, has_sample FROM gaps WHERE NOT archived ORDER BY account_id, day DESC),
age AS (SELECT *, DATE '2026-10-27'-day a FROM ld)
SELECT owner, count(*) FILTER (WHERE has_quote OR has_sample) last_is_qs_all,
 count(*) FILTER (WHERE (has_quote OR has_sample) AND a BETWEEN 8 AND 30) d8_30,
 count(*) FILTER (WHERE (has_quote OR has_sample) AND a BETWEEN 31 AND 90) d31_90,
 count(*) FILTER (WHERE (has_quote OR has_sample) AND a BETWEEN 91 AND 180) d91_180,
 count(*) FILTER (WHERE (has_quote OR has_sample) AND a > 180) d181p
FROM age GROUP BY ROLLUP(owner) ORDER BY 1
```
关键词扩展版（Q9）：
```sql
<BASE>
, ld AS (
  SELECT account_id, owner, day,
    bool_or(typ IN ('quote','sample') OR <TXT> ~ '报价|quotation|\mquote|样品|寄样|\msample|\mpi\M|proforma') qs
  FROM ev e WHERE NOT archived AND day = (SELECT max(day) FROM ev e2 WHERE e2.account_id=e.account_id AND NOT e2.archived)
  GROUP BY 1,2,3
), age AS (SELECT *, DATE '2026-10-27'-day a FROM ld)
SELECT owner, count(*) accts,
 count(*) FILTER (WHERE a BETWEEN 8 AND 14) all_8_14, count(*) FILTER (WHERE a BETWEEN 15 AND 30) all_15_30,
 count(*) FILTER (WHERE a BETWEEN 31 AND 90) all_31_90, count(*) FILTER (WHERE a>90) all_91p,
 count(*) FILTER (WHERE qs AND a BETWEEN 8 AND 14) qs_8_14, count(*) FILTER (WHERE qs AND a BETWEEN 15 AND 30) qs_15_30,
 count(*) FILTER (WHERE qs AND a BETWEEN 31 AND 90) qs_31_90, count(*) FILTER (WHERE qs AND a BETWEEN 91 AND 180) qs_91_180,
 count(*) FILTER (WHERE qs AND a>180) qs_181p
FROM age GROUP BY ROLLUP(owner) ORDER BY 1
```

Q10 报价/寄样联系日的月度数量（说明类型标签 7 月后消失）与稳态发射率：
```sql
<BASE>
SELECT to_char(day,'YYYY-MM') m, count(*) all_days, count(*) FILTER (WHERE has_quote OR has_sample) qs_days
FROM gaps WHERE NOT archived AND day >= DATE '2025-01-01' GROUP BY 1 ORDER BY 1
```
```sql
<BASE>
SELECT coalesce(owner,'ALL') owner, count(*) qs_days_18mo, round(count(*)/18.0,1) per_month,
 round(100.0*count(*) FILTER (WHERE next_day IS NULL OR next_day-day>14)/count(*)) silent14_pct,
 round(count(*) FILTER (WHERE next_day IS NULL OR next_day-day>14)/18.0/22,2) emitted_per_workday
FROM gaps WHERE NOT archived AND (has_quote OR has_sample) AND day >= DATE '2025-01-01' AND day < DATE '2026-07-01'
GROUP BY ROLLUP(owner) ORDER BY 1
```

Q11 丙：成交信号（关键词，带否定排除）与老客户标记，按最后联系年龄：
```sql
<BASE>
, deal AS (
  SELECT account_id, min(day) first_deal_day FROM ev
  WHERE NOT archived AND <TXT> ~ '下单|订单|\mpo\M|purchase order|proforma|定金|订金|deposit|尾款|已付|付款|到账|出货|发货|订舱|装柜|shipped|复购|返单|repeat order|re-order|reorder'
    AND <TXT> !~ '没有订单|没订单|无订单|暂无订单|未下单|没下单|没有下单|无下单|no order|not order|暂无下单|还没.{0,3}订单|未付款|没付款|没有付款'
  GROUP BY 1),
ld AS (SELECT DISTINCT ON (account_id) account_id, owner, day FROM gaps WHERE NOT archived ORDER BY account_id, day DESC),
j AS (SELECT ld.owner, DATE '2026-10-27'-ld.day a, d.first_deal_day IS NOT NULL is_deal, ac.customer_type='existing' is_existing
      FROM ld JOIN trosa.accounts ac ON ac.id=ld.account_id LEFT JOIN deal d ON d.account_id=ld.account_id)
SELECT owner, count(*) FILTER (WHERE is_deal) deal_accts,
 count(*) FILTER (WHERE is_deal AND a>60) gt60, count(*) FILTER (WHERE is_deal AND a>90) gt90, count(*) FILTER (WHERE is_deal AND a>180) gt180,
 count(*) FILTER (WHERE is_existing) existing_with_rec, count(*) FILTER (WHERE is_existing AND a>60) ex_gt60,
 count(*) FILTER (WHERE is_existing AND a>90) ex_gt90, count(*) FILTER (WHERE is_existing AND a>180) ex_gt180
FROM j GROUP BY ROLLUP(owner) ORDER BY 1
```
（上式 `<TXT>` 为实际 SQL 里展开的完整表达式；查询里 `\m`/`\M` 是 PostgreSQL 的词边界。）

Q12 丁：未来时间说法（最近 30 天计数、最后一条且已过 3/7 天）。关键词正则：
`下周|下星期|下礼拜|下个星期|next week|coming week`（周）、`下个月|下月|月底|月末|月中|月初|next month|end of (the |this |next )?(month|week|year)|mid[- ]?month`（月）、`节后|春节|过年|斋月|开斋|宰牲|圣诞|新年|国庆|排灯|ramadan|\meid\M|diwali|holiday|christmas|new year|\mcny\M`（节日）、`[0-9一二两三四五六七八九十]+ ?(天|周|个星期|个月|星期)后|in (a|one|two|three|2|3|4|few|a few|[0-9]+) (day|days|week|weeks|month|months)|few weeks|couple of weeks`（N 天/周后）。
```sql
<BASE>
, t AS (SELECT owner, account_id, day, <TXT> txt FROM ev WHERE NOT archived),
g AS (SELECT *, (txt ~ '<周>' OR txt ~ '<月>' OR txt ~ '<节日>' OR txt ~ '<N后>') anyp FROM t),
lastday AS (SELECT account_id, max(day) md FROM t GROUP BY 1)
SELECT owner,
 count(*) FILTER (WHERE day >= DATE '2026-09-27') recs30,
 count(*) FILTER (WHERE day >= DATE '2026-09-27' AND anyp) recs30_phrase,
 count(DISTINCT account_id) FILTER (WHERE day >= DATE '2026-09-27' AND anyp) accts30_phrase,
 count(DISTINCT account_id) FILTER (WHERE anyp AND day >= DATE '2026-09-12' AND day=(SELECT md FROM lastday l WHERE l.account_id=g.account_id) AND DATE '2026-10-27'-day >= 7) pending_7p,
 count(*) FILTER (WHERE day >= DATE '2026-04-27' AND day < DATE '2026-09-03' AND anyp) hist180_phrase,
 count(*) FILTER (WHERE day >= DATE '2026-04-27' AND day < DATE '2026-09-03') hist180_recs
FROM g GROUP BY ROLLUP(owner) ORDER BY 1
```

Q13 合并库存（去重）：在 Q6/Q9/Q11/Q12 的“最后一天”快照上并列四个条件，`union_all` 为四个条件任一成立：
`(c AND NOT u AND NOT tw AND a BETWEEN 2 AND 30) OR (qs AND a BETWEEN 8 AND 90) OR (is_deal AND a BETWEEN 61 AND 365) OR (ph AND a BETWEEN 3 AND 45)`（`ph`＝最后一天含未来说法）。

探查（决定“成交”如何识别）：`trosa.timeline_events` 按 `event_type`×`source_module` 计数；`trosa.customer_states.business_stage`、`trosa.accounts.account_status/customer_type/priority_level/attention_state` 计数；对 13 组关键词（订单、PO、PI、定金、尾款、付款、出货等）统计命中的记录数与客户数（订单 43 个客户、出货/发货/订舱 45、付款 24、下单 14、定金 6、PI 6、复购 2）。

### 第四节用到的查询

Q14 成交（关键词锚点）vs 变冷（最后联系 >90 天且无成交信号）在锚点前的节奏：
```sql
<BASE>
, dealfirst AS (
  SELECT account_id, min(day) anchor FROM ev
  WHERE NOT archived AND <TXT> ~ '下单|订单|\mpo\M|purchase order|proforma|定金|订金|deposit|尾款|已付|付款|到账|出货|发货|订舱|装柜|shipped|复购|返单|repeat order|re-order|reorder'
    AND <TXT> !~ '没有订单|没订单|无订单|暂无订单|未下单|没下单|没有下单|无下单|no order|not order|暂无下单|还没.{0,3}订单|未付款|没付款|没有付款'
  GROUP BY 1),
anch AS (
  SELECT account_id, 'deal' grp, anchor FROM dealfirst
  UNION ALL
  SELECT account_id, 'cold', max(day) FROM cd WHERE NOT archived AND account_id NOT IN (SELECT account_id FROM dealfirst)
  GROUP BY 1 HAVING DATE '2026-10-27'-max(day)>90),
w AS (
  SELECT a.account_id, a.grp, a.anchor, c.owner,
   count(c.day) FILTER (WHERE c.day < a.anchor) n_before,
   count(c.day) FILTER (WHERE c.day < a.anchor AND c.day >= a.anchor-28) n_4w,
   count(c.day) FILTER (WHERE c.day < a.anchor AND c.day >= a.anchor-56) n_8w,
   coalesce(bool_or(c.has_quote) FILTER (WHERE c.day<a.anchor AND c.day>=a.anchor-90),false) q90,
   coalesce(bool_or(c.has_sample) FILTER (WHERE c.day<a.anchor AND c.day>=a.anchor-90),false) s90,
   a.anchor - max(c.day) FILTER (WHERE c.day<a.anchor) last_gap
  FROM anch a JOIN cd c ON c.account_id=a.account_id AND NOT c.archived
  GROUP BY a.account_id,a.grp,a.anchor,c.owner)
SELECT grp, coalesce(owner,'ALL') owner, count(*) n_accts, count(*) FILTER (WHERE n_before>=2) n_ge2,
 percentile_cont(0.5) WITHIN GROUP (ORDER BY n_before) FILTER (WHERE n_before>=2) med_total_before,
 percentile_cont(0.5) WITHIN GROUP (ORDER BY n_8w) FILTER (WHERE n_before>=2) med_8w,
 round(avg(n_8w) FILTER (WHERE n_before>=2),2) avg_8w,
 round(100.0*count(*) FILTER (WHERE n_before>=2 AND n_4w>=1)/nullif(count(*) FILTER (WHERE n_before>=2),0)) pct_contact_last4w,
 round(100.0*count(*) FILTER (WHERE n_before>=2 AND n_8w>=2)/nullif(count(*) FILTER (WHERE n_before>=2),0)) pct_ge2_in8w,
 percentile_cont(0.5) WITHIN GROUP (ORDER BY last_gap) FILTER (WHERE n_before>=2) med_last_gap,
 round(100.0*count(*) FILTER (WHERE n_before>=2 AND q90)/nullif(count(*) FILTER (WHERE n_before>=2),0)) pct_quote90,
 round(100.0*count(*) FILTER (WHERE n_before>=2 AND s90)/nullif(count(*) FILTER (WHERE n_before>=2),0)) pct_sample90
FROM w GROUP BY GROUPING SETS ((grp),(grp,owner)) ORDER BY 1,2
```

Q15 变冷前兆：最后一个间隔 vs 平时间隔（`anch` 同 Q14，把 `w` 换成下面的 `seq`、`z`）：
```sql
..., seq AS (
  SELECT a.account_id, a.grp, a.anchor, c.day, c.day - LAG(c.day) OVER (PARTITION BY a.account_id, a.grp ORDER BY c.day) g,
         row_number() OVER (PARTITION BY a.account_id, a.grp ORDER BY c.day DESC) rn
  FROM anch a JOIN cd c ON c.account_id=a.account_id AND NOT c.archived AND c.day < a.anchor),
z AS (
  SELECT account_id, grp, anchor, max(anchor-day) FILTER (WHERE rn=1) last_gap,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY g) FILTER (WHERE rn>1 AND g IS NOT NULL) earlier_med, count(*) n
  FROM seq GROUP BY 1,2,3)
SELECT grp, count(*) n_ge3,
 percentile_cont(0.5) WITHIN GROUP (ORDER BY last_gap) med_last_gap,
 percentile_cont(0.5) WITHIN GROUP (ORDER BY earlier_med) med_earlier_gap,
 round(100.0*count(*) FILTER (WHERE last_gap > 2*greatest(earlier_med,7))/count(*)) pct_last_gt_2x,
 round(100.0*count(*) FILTER (WHERE last_gap > 30)/count(*)) pct_last_gt30
FROM z WHERE n>=3 AND earlier_med IS NOT NULL GROUP BY 1
```

### 第五节用到的查询

Q16 总体规模（非归档/归档、无时刻、重复事件、批量日期、类型方向稀薄度、热门日期）：
```sql
<BASE>
SELECT 'events_total' k, count(*)::text v FROM ev
UNION ALL SELECT 'events_archived_accts', count(*)::text FROM ev WHERE archived
UNION ALL SELECT 'accts_with_events_active', count(DISTINCT account_id)::text FROM ev WHERE NOT archived
UNION ALL SELECT 'accts_with_events_archived', count(DISTINCT account_id)::text FROM ev WHERE archived
UNION ALL SELECT 'archived_accts_total', count(*)::text FROM trosa.accounts WHERE deleted_at IS NOT NULL
UNION ALL SELECT 'active_accts_total', count(*)::text FROM trosa.accounts WHERE deleted_at IS NULL
UNION ALL SELECT 'archived_with_event_last90d', count(DISTINCT account_id)::text FROM ev WHERE archived AND day >= DATE '2026-07-29'
UNION ALL SELECT 'accts_one_contact_day', count(*)::text FROM (SELECT account_id FROM cd WHERE NOT archived GROUP BY 1 HAVING count(*)=1) x
UNION ALL SELECT 'accts_ge2_contact_days', count(*)::text FROM (SELECT account_id FROM cd WHERE NOT archived GROUP BY 1 HAVING count(*)>=2) x
UNION ALL SELECT 'events_same_acct_day_ge5(events)', coalesce(sum(n),0)::text FROM (SELECT count(*) n FROM ev WHERE NOT archived GROUP BY account_id, day HAVING count(*)>=5) x
UNION ALL SELECT 'dir_unknown_events', count(*) FILTER (WHERE who='unknown')::text || ' of ' || count(*) FROM ev WHERE NOT archived
UNION ALL SELECT 'generic_followup_type', count(*) FILTER (WHERE typ='follow_up')::text || ' of ' || count(*) FROM ev WHERE NOT archived
UNION ALL SELECT 'top_date_'||(row_number() OVER (ORDER BY n DESC))::text, day::text||' n='||n::text FROM (SELECT day, count(*) n FROM ev WHERE NOT archived GROUP BY 1 ORDER BY 2 DESC LIMIT 6) t
```
（各业务员无记录客户数：`trosa.accounts` 非归档数减去有记录数；`events_by_src_*` 为按 `src` 分组计数。）

Q17 批量日期、时刻、更正、周一占比、重复事件：
```sql
<BASE>
, od AS (SELECT owner, day, count(*) n FROM ev WHERE NOT archived GROUP BY 1,2),
bulk AS (SELECT owner, day FROM od WHERE n>=25)
SELECT 'bulk_days' k, count(*)::text v FROM bulk
UNION ALL SELECT 'events_on_bulk_days', coalesce(sum(n),0)::text || ' of ' || (SELECT sum(n) FROM od) FROM od JOIN bulk USING (owner, day)
UNION ALL SELECT 'bulk_days_by_owner_'||owner, count(*)::text||' days / '||sum(n)::text||' events' FROM od WHERE n>=25 GROUP BY owner
UNION ALL SELECT 'midnight_events', (SELECT count(*) FILTER (WHERE (e.occurred_at AT TIME ZONE 'Asia/Shanghai')::time = TIME '00:00')::text||' of '||count(*) FROM trosa.timeline_events e WHERE lower(coalesce(e.payload->>'is_deleted','0')) NOT IN ('1','true') AND e.event_type NOT IN ('outreach_bounced','外联退信','outreach_email','task_completed','周内汇总'))
UNION ALL SELECT 'date_corrected_events', (SELECT count(*)::text FROM trosa.timeline_events e WHERE e.payload ? 'date_corrected_from')
UNION ALL SELECT 'weekday_mon_share_history', (SELECT round(100.0*count(*) FILTER (WHERE extract(dow FROM day)=1)/count(*))::text FROM ev WHERE NOT archived AND day < DATE '2026-09-03')
UNION ALL SELECT 'exact_dup_events(same acct,day,content)', (SELECT coalesce(sum(n-1),0)::text FROM (SELECT count(*) n FROM ev WHERE NOT archived AND coalesce(content,'')<>'' GROUP BY account_id, day, content HAVING count(*)>1) x)
```
（注意：本工具把 SQL 当作带参数的语句，字符串里的百分号要写成 `%%`；上面刻意避开了。）

Q18 重复客户（名称归一后重名；邮箱重复）：
```sql
WITH a AS (SELECT a.id, u.username owner, regexp_replace(lower(a.display_name),'[^a-z0-9一-鿿]','','g') nm,
   EXISTS (SELECT 1 FROM trosa.timeline_events e WHERE e.account_id=a.id AND e.event_type NOT IN ('outreach_bounced','外联退信','outreach_email','task_completed','周内汇总')) has_ev
  FROM trosa.accounts a JOIN identity.users u ON u.id=a.owner_user_id WHERE a.deleted_at IS NULL),
g AS (SELECT nm, count(*) n, count(DISTINCT owner) owners, count(*) FILTER (WHERE has_ev) with_ev FROM a WHERE length(nm)>=3 GROUP BY nm HAVING count(*)>1),
c AS (SELECT lower(trim(cc.email)) em, count(DISTINCT cc.customer_id) n FROM trosa.customer_contacts cc JOIN trosa.customers cu ON cu.id=cc.customer_id AND coalesce(cu.is_deleted,0)=0 WHERE cc.email LIKE '%%@%%' GROUP BY 1 HAVING count(DISTINCT cc.customer_id)>1)
SELECT count(*) groups, sum(n) accounts, count(*) FILTER (WHERE owners>1) cross_owner_groups, count(*) FILTER (WHERE with_ev>=2) groups_ge2_with_events, (SELECT count(*) FROM c) dup_email_groups FROM g
```

Q19 Sela 记录混入、汇总字段（`last_contact_at`）核对：
```sql
<BASE>
, raw AS (SELECT e.account_id, (e.occurred_at AT TIME ZONE 'Asia/Shanghai')::date d, e.event_type FROM trosa.timeline_events e JOIN trosa.accounts a ON a.id=e.account_id AND a.deleted_at IS NULL WHERE lower(coalesce(e.payload->>'is_deleted','0')) NOT IN ('1','true')),
b AS (SELECT DISTINCT account_id FROM raw WHERE event_type IN ('outreach_bounced','外联退信')),
real_last AS (SELECT account_id, max(day) d FROM ev WHERE NOT archived GROUP BY 1),
all_last AS (SELECT account_id, max(d) d FROM raw GROUP BY 1)
SELECT 'bounce_accounts' k, count(*)::text v FROM b
UNION ALL SELECT 'bounce_accounts_with_no_real_event', count(*)::text FROM b WHERE account_id NOT IN (SELECT account_id FROM real_last)
UNION ALL SELECT 'bounce_is_latest_event', count(*)::text FROM b JOIN real_last r USING (account_id) JOIN all_last l USING (account_id) WHERE l.d > r.d
UNION ALL SELECT 'customer_reply_events_autoreply_kw', count(*)::text FROM ev WHERE NOT archived AND who='customer' AND lower(coalesce(content,'')||' '||coalesce(result,'')) ~ 'out of office|auto-?reply|automatic reply|自动回复|自动回覆|休假|vacation|on leave|delivery status|undeliver'
UNION ALL SELECT 'last_contact_at_set_but_no_event', count(*)::text FROM trosa.accounts a WHERE a.deleted_at IS NULL AND a.last_contact_at IS NOT NULL AND a.id NOT IN (SELECT account_id FROM real_last)
UNION ALL SELECT 'last_contact_at_differs_gt7d', count(*)::text FROM trosa.accounts a JOIN real_last r ON r.account_id=a.id WHERE a.deleted_at IS NULL AND a.last_contact_at IS NOT NULL AND abs((a.last_contact_at AT TIME ZONE 'Asia/Shanghai')::date - r.d) > 7
UNION ALL SELECT 'outreach_rows / distinct customers', (SELECT count(*)::text||' / '||count(DISTINCT customer_id)::text FROM trosa.outreach_emails)
```
