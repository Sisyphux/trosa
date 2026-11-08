# Trosa 现状调研（产品、理念、投入去向、真实使用）

> 调研日期 2026-10-27。只读：读了仓库文档、`git log`、`CHANGELOG.md`、路由与前端片段，并用维护者只读通道（`tools/trosa_maint_read.py`，只读数据库角色 `trosa_maint_ro`）查了**汇总统计**。没有抄录任何客户名、邮箱、电话或沟通原文。
> 来源写法：仓库文件用相对路径（根目录 = `/Users/luoxin/Desktop/Trosa/.claude/worktrees/undesigned-pages-audit-f0e651/`）；数据库统计统一标为 [只读库查询 2026-10-27](tools/trosa_maint_read.py)。
> 数据口径提醒（很重要）：
> - 2026-09-03 切换到 PostgreSQL 时，历史记录的 `created_at` 被统一重写成迁移日，所以“按创建时间”统计会看到一个 8/31 那周的大峰值（迁移，不是使用）。下面看“真实使用”时改用 `follow_date`（沟通发生日）和操作日志 `audit.operation_log_events`（9 月 14 日起才有）。
> - `trosa.inbox_items` 和 `inbox_threads` 不按成员区分，看到的是全体合计。
> - 系统里的活跃账号是 **amy、emma、hamid、kelley 四个**（carl、kelly 为停用）。文档里多处只写 Hamid/Amy/Kelley 三人，Emma 没被写进去。

## 一、现在有哪些页面/模块，各自让业务员做什么

### Takeaway
Trosa 是一个“客户 + 联系人 + 沟通记录 + 带日期待办”的极简关系工作台，外加两块重投入的部分：给 Sela 自动开发用的 Inbox 对话窗口，以及“本周工作”周报页。主导航只有 今天 / Inbox / 客户 / 本周工作，其余（日历、全部记录、操作日志、设置）是二级入口。没有报价、样品、订单、付款这些业务对象。

### Cited Findings
- 产品定位一句话：“外贸客户关系工作台。它保存关系上下文、呈现已到期承诺，并帮助业务员把每次实际进展沉淀为可追溯记录。”四个长期对象：客户（customers）、联系人（contacts）、沟通记录（follow_up_logs、outreach_emails、Sela 证据）、待办（reminders）。— [PRODUCT_DIRECTION.md](PRODUCT_DIRECTION.md)
- 主导航：今天、Inbox、客户、本周工作；新客户、完整日历、全部记录、操作日志、设置在“场景入口”；“新增一级页面需证明现有页面、面板或动作无法清楚承载”。— [PRODUCT_DESIGN_STANDARD.md §5](PRODUCT_DESIGN_STANDARD.md)
- **今天（潮汐）**：页面上方一条“潮线”，横轴是我们这边的 0–24 点，曲线是此刻有多少客户处在当地工作时间（09–18、周一至周五）；每个客户一行，显示名字与下一步、对方工作窗口、倒计时；分“窗口已开 / 稍后开窗 / 今天没窗口 / 时区未知”。右栏“接下来 14 天”。待办只有日期没有时刻。— [design/DESIGN_WORLD.md §3](design/DESIGN_WORLD.md)
- **Inbox（对话）**：入口始终打开“与 sela 的对话”（左列表、右消息与回复框）；“其他事项”页签（客户回复捕获、待确认主体等）只在有内容时出现。— [design/DESIGN_WORLD.md §3](design/DESIGN_WORLD.md)；Inbox 由“问题卡片队列”在 10 月初改成对话形态 — git log `[inbox-dialogue-backend]` `[s3-inbox-dialogue-ui]` `[inbox-dialogue-merge]`（2026-10-05 至 10-08）
- **客户列表（账页）**：按“最近发生”分段的长卷（今天/昨天/本周/本月/1–3 个月/更早/尚无记录），每条两行。**客户工作区**：顶部分区（最近沟通/资料/联系人/文件/待办），中心是“最近沟通 | 一句下一步”，底栏“当前等待”，唯一实心按钮是“记录沟通”。— [design/DESIGN_WORLD.md §3](design/DESIGN_WORLD.md)
- **本周工作**：每人从自己记录里勾选要分享的内容（“同步到周报”勾选 + 分类：重点跟进/新客户/报价进展/样品寄送/订单签约）；10 月 22 日改为“四人同页、按人成带、轮讲视图”，并在每条沟通前交代客户是谁（行业、业务介绍自动译中文等）；明确不做排名、KPI。— [app/static/index.html 第 955–972 行](app/static/index.html)；[PRODUCT_DESIGN_STANDARD.md §12](PRODUCT_DESIGN_STANDARD.md)；git log `[weekly-columns]` `[weekly-design]`（2026-10-22）
- **记录沟通弹窗**：沟通方式可选 WhatsApp、邮件、电话、会议、报价、寄样、展会现场、微信、视频会议、客户来访、其他跟进；可顺手填“下一步做什么？”+日期（10 月 13 日起 今天/Inbox/客户页共用）。— [app/static/index.html 第 781–785 行](app/static/index.html)；git log `[today-next-step]` 2026-10-13
- **客户文件**：可上传报价单、合同、样品图片等，单文件 ≤25MB，可预览。— [使用说明.md](使用说明.md)
- **可选 AI**：沟通整理（粘贴文字/WhatsApp 导出）、截图识别、官网识别导入、邮箱可发送性复核；设置里配置共享 API（DeepSeek、通义、GLM、OpenAI 兼容、本地模型）。— [使用说明.md](使用说明.md)、[PRODUCT_DIRECTION.md](PRODUCT_DIRECTION.md)
- **浏览器扩展**：Chrome/Edge MV3 侧边栏，需“开发者模式→加载已解压扩展”安装；只采集当前打开页面：网易邮箱当前线程、WhatsApp Web 已加载消息；保存前必须确认。适配器只有 `netease.js`、`whatsapp.js`。— [browser-extension/README.md](browser-extension/README.md)
- **Sela 集成**：Sela 负责发开发信、读邮箱识别回信/退信，经 `/api/integrations/sela/*` 交给 Trosa；精确身份匹配 + 幂等键 + 歧义走 REVIEW。— [AGENTS.md](AGENTS.md)、[docs/SYSTEM_STATE.md §3](docs/SYSTEM_STATE.md)
- 路由按前缀计数：`/api/customers` 40、`/api/integrations` 32、`/api/inbox` 19、`/api/gateway` 12、`/api/reminders` 9、`/api/agent` 8（grep `@app.route`）。`app.py` 现 23,113 行、`app/static/app.js` 15,205 行。— [app.py](app.py)、[app/static/app.js](app/static/app.js)
- 架构：单台阿里云 ECS、单进程 Flask/Waitress、本机 PostgreSQL、Cloudflare Tunnel、原生 JS 单页无构建。— [docs/SYSTEM_STATE.md §2](docs/SYSTEM_STATE.md)
- 数据库迁移里没有报价单、订单、商机、发票、付款、出货这类表（对 `migrations/*.sql` grep `CREATE TABLE ... order|deal|quot|invoice|opportunit|sample|shipment|payment` 无结果）；“报价/寄样/订单”只以“沟通方式”、文件分类和周报分类标签的形式存在。— [migrations/](migrations/)、[app/static/index.html](app/static/index.html)

### Inferences
- 业务员在 Trosa 里能做的事本质是三件：找客户、写一条沟通、（可选）挂一个带日期的下一步。外贸真正的主线——询盘 → 报价 → 打样 → 下单 → 收款 → 出货——在系统里没有结构，只能散落在自由文字里。
- Inbox 现在几乎是“Hamid 和 Sela 之间的工作窗口”，对另外三位同事意义不大（见第五节数据）。

### Gaps
- 没有在真实浏览器里逐页走一遍（本次只读文档和代码）；页面实际观感、空状态在同事账号下的样子未验证。
- 文档之间有漂移：`docs/SYSTEM_STATE.md` 仍写 158 条路由、app.py 约 1.5 万行、迁移 0001–0029，实际迁移已到 0121、app.py 约 2.3 万行；`AGENTS.md` 说“下一项优先级是 Inbox 与 Search 续接”，而 `TROSA_MAINTENANCE.md` 的表格写该项已发布。

## 二、产品理念是什么，哪些是长处、哪些可能在制造摩擦

### Takeaway
理念非常一致且克制：“恢复上下文 → 执行已确认动作 → 记录事实 → 按需确认带日期的下一步”；事实/承诺/待判断/建议严格分层；不评分、不自动发消息、不自动建待办、AI 只做可关闭的阅读层。它把“不出错、不制造假压力、数据安全”做到了极致，但代价是：系统几乎不主动替人干活，日常价值高度依赖业务员自觉手工录入，而数据显示同事并没有这么用。

### Cited Findings（理念原文）
- 闭环与两条铁律：“沟通记录可以没有下一步；待办必须同时有明确动作和日期。”判断设计的五条标准：长期主义、简洁、真正解决现实问题、AI-native、稳定容错。— [docs/SYSTEM_STATE.md §1](docs/SYSTEM_STATE.md)
- “信息压力规则”：已发生的事实→客户时间线；需要判断的新信号→Inbox；明确的未来承诺（有动作+日期）→今天/日历；可能值得关注→动态视图或可选建议、**不自动生成逾期**；日常无需再看→归档。“等待回复、很久未联系、没有下一步”等状态从事实动态计算，不让人维护。— [PRODUCT_DIRECTION.md](PRODUCT_DIRECTION.md)
- 用户负责“事实、判断和行动”；系统负责“保存、组织、提醒和保护数据”；可选辅助负责“读取与整理”。— [PRODUCT_DESIGN_STANDARD.md §1](PRODUCT_DESIGN_STANDARD.md)
- AI 边界：可关闭的辅助阅读层；输出区分事实/推断/未知；“模型输出不会直接改写客户、联系人、记录或待办”；未配置模型时核心完整可用。— [PRODUCT_DIRECTION.md](PRODUCT_DIRECTION.md)
- 无人值守的自动化（Sela、AI 预填、定时任务）“不得自动创建客户、联系人、待办或商业承诺”；成员本人指令下持 `crm:write` 令牌的 Agent 可以直接写。— [AGENTS.md](AGENTS.md)
- 长期边界：“不引入数字评分式客户优先级、自动发送消息、复杂审批流、第三方模块市场和大规模权限体系。”— [PRODUCT_DIRECTION.md](PRODUCT_DIRECTION.md)
- 不重新引入大量客户状态、等级、标签、复杂视图或批量管理作为前台负担。— [AGENTS.md](AGENTS.md)
- 客户/联系人自动匹配只认“规范化后的唯一精确邮箱或手机号”，其余进候选/待审阅。— [AGENTS.md](AGENTS.md)
- 界面：“一屏一个焦点”、整屏只有一个实心主按钮、“文字减法”（只放数据和动作）、说人话；瑞士式白底黑字 + 一个橙红强调色 +“一笔”手绘图标。— [design/DESIGN_WORLD.md §1–2](design/DESIGN_WORLD.md)、[PRODUCT_DESIGN_STANDARD.md §13](PRODUCT_DESIGN_STANDARD.md)
- 维护路线图自己的判断：“当前最有价值的改进不是新增页面，而是把分散在 Customer、Today、Inbox 和 Search 的同一次沟通收敛为一个可确认的入口。”原问题描述：“用户重复选择渠道、方向、日期、客户和下一步；同一事实也可能被记录两次。”— [TROSA_MAINTENANCE.md](TROSA_MAINTENANCE.md)

### Cited Findings（与理念相关的使用事实，详见第五节）
- 9 月 5 日以后，Kelley 和 Emma 手动建的待办都是 **0 条**，Amy 是 **2 条**，Hamid 是 57 条（完成 41）。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)
- 近 30 天沟通记录里填了“下一步”的：Amy 38 条中 1 条，Kelley 28 条中 0 条，Emma 6 条中 0 条，Hamid 123 条中 7 条。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)
- 同事的录入集中在周一：近 60 天 Kelley 59 次“记录沟通”中 38 次在周一；Amy 58 次中 25 次周一、21 次周三。Kelley 9 月 10 日以来的手工记录有 85% 勾了“同步到周报”，Amy 为 52%。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)

### Inferences（评判）
**明显的长处（值得保留、是差异点）**
- “事实 / 承诺 / 待判断 / 建议”分层，加上“不自动生成逾期、不打分”，避免了传统 CRM 那种红点满屏、没人信的状态。
- 数据安全与可追溯（归档可恢复、来源审计、撤销、每次写入有回执）符合老板“客户数据很重要”的要求。
- 身份匹配只认精确邮箱/手机号，避免 AI 把客户串错——对多人共用客户池是对的。
- 时区“潮汐”是真正贴合外贸的设计：客户在美洲、中东、印度，知道“现在谁在上班”很有用。
- 周报“四人同页、按人轮讲”贴合了团队真实的周会场景，数据也证明这是同事最常用的部分。

**可能在制造摩擦的地方**
1. **核心闭环依赖“人手动写下一步”，但同事不写。** 设计上“下一步可选”，结果同事几乎从不挂待办 → 他们的“今天”页基本是空的（Kelley、Emma 当前 0 条未完成待办）→ 每天打开没有理由。今天页的潮汐再好看，也没有内容可显示。
2. **“只记录事实”= 录入成本全在人身上。** 同事的典型行为是周一集中补录、为周会写周报（录入延迟中位数 0–1 天，但 Amy 有 47% 记录是事后 ≥2 天补的）。系统对他们来说是“周报填写器”，不是“每天的工作台”。
3. **AI 被放得太远。** 理念把 AI 定位成“可选、可关闭、只读”。可真实的痛点恰恰是“把 WhatsApp/邮件里发生的事变成记录”和“告诉我今天该回谁”——这正是 AI 最擅长、风险也最可控（只预填、人确认）的部分。现在 AI 能力藏在弹窗的“沟通整理/识图”按钮后面，不是默认路径。（注：10-09 有一条 commit“沟通记录入库前先用 Trosa 的 AI 整理归纳”，说明实际已在往“默认用 AI”靠，但文档理念还没跟上。）
4. **没有报价/样品/订单/付款对象。** 外贸业务员最关心的“这个客户报到哪个价了、样品寄了没、PI 签了没、尾款收了没”在系统里只能靠翻自由文字或文件。周报里有“报价进展/样品寄送/订单签约”分类，说明团队心里有这条主线，但系统没给它结构。
5. **WhatsApp 是主要渠道却没有顺畅的捕获。** 扩展要开发者模式安装、只支持网易邮箱和 WhatsApp Web；近 30 天同事记录里标成 WhatsApp 的只有 Kelley 5 条。没有手机端捕获（业务员大量 WhatsApp 在手机上）。
6. **“不加概念、不加状态”有点矫枉过正。** 把等级、阶段、标签都降权是对的，但“客户处于哪个商务阶段（询盘/报价/打样/下单/复购）”对外贸是刚需信息，不是负担。
7. **投入与受益人错位。** 大量投入在 Sela 的 Inbox 对话、身份歧义、退信处理上，受益者基本只有 Hamid（Inbox 人类消息 49 条全部来自 hamid）。

### Gaps
- 没有访谈同事，不知道他们不写下一步是“不需要”“嫌麻烦”还是“不知道这功能”。
- 无法确认团队周会是否固定在周一（只能从录入集中在周一推测）。

## 三、开发投入去了哪里

### Takeaway
最近 300 个 commit（2026-09-18 至 10-27，约 40 天，平均每天 7–8 个）里：界面视觉/文案约 27%，Sela 集成约 25%，发布/基础设施/多 Agent 开发工具约 21%，Inbox（主要是 Sela 对话）约 18%，其余面向同事日常的功能和数据修正只有约 10%。也就是说，大约 2/3 的开发力气花在“外观”和“Sela + 运维流水线”上，真正让同事每天更省事的新能力很少。

### Cited Findings
- 仓库共 452 个 commit，首个 2026-06-26（“初始化 CRM 客户跟进提醒系统”）；按月：6 月 5、8 月 25、9 月 256、10 月 166。— `git log`（本仓库）
- 最近 300 个 commit 的时间范围 2026-09-18 → 2026-10-27。按 `[task-id]` + 标题关键词粗分（同一 commit 只归一类，属近似）：
  - 视觉/设计/文案/可访问性：80（例：`ui-daylight-perpage` 10、`complete-modal-polish-0930` 11、`swiss-type-explore*` 6、`hand-*`「一笔」图标 5 期、10-22 一天内 `td/cl/cw/ib/st/md/hl/tap/fmt-polish` 多个小幅优化）
  - Sela 集成（外联、退信、身份、排除清单、读写接口性能）：74（例：`s9-interface-efficiency` 7、`sela-*` 二十多个不同任务）
  - 发布/基础设施/多 Agent 工具/验收脚本/文档清理：64（例：`release-flow-refactor` 5、`release-network-resilience` 4、`worktree-lease-0930` 4、`parallel-dev-isolation` 3、`agent-dev-ergonomics` 3）
  - Inbox（问题队列 → 对话）：53（例：`inbox-human-intervention-workspace` 21、`inbox-dialogue-backend` 7、`s3-inbox-dialogue-ui` 6、`inbox-decision-contract` 5）
  - 其他功能/数据修正：29（搜索容错/同义词/拼音、客户时区、完成待办可撤销、补录旧日期不被挤掉、Kelley 历史记录年份修正、清洗邮件引用历史等）
  - 标题含“修复/fix/不再/修正/误”等字样的约 65 个（与上面分类交叉）。
  — `git log -300`（本仓库），分类为本次脚本粗分
- 视觉语言在约 4 周内换了多次：09-27 统一为 “Daylight”；09-28~30 “房间”方案（Inbox=灯箱、Today=潮汐、客户=账页）；10-09 瑞士式设为默认并取消性能分档；10-13 删除旧设计文档与原型；10-13~15 “一笔”手绘图标五期；10-22 全站小幅打磨；10-27 删掉旧外观开关。— git log；[design/DESIGN_WORLD.md](design/DESIGN_WORLD.md) 开头说明
- 测试文件 67 个，其中 `release_*` 9 个、`sela_*` 约 13 个、`inbox_*` 约 6 个；直接以 today/customer 命名的很少（`today_dedupe_regression`、`today_timezone`、`customer_*` 几个）。— [tests/](tests/)
- 根目录 `CHANGELOG.md` 3,480 行、约 481 个标题，最近条目以“小幅优化”“不再显示…”“看得清”为主。— [CHANGELOG.md](CHANGELOG.md)
- 多 Agent 并行开发有一整套制度（worktree 租约、角色边界、迁移编号锁、发布证据），写在 AGENTS.md 和 MULTI_AGENT_WORKFLOW.md。— [AGENTS.md](AGENTS.md)

### Inferences
- 投入结构反映的是“老板本人 + Sela 自动开发”的需求，而不是“同事每天用”的需求。Sela/Inbox 合计约 127 个 commit（≈42%），而 Inbox 的人类使用者只有 Hamid 一人。
- 视觉的反复重做说明“好看”被当成了提升使用率的主要手段，但同事的使用数据在这段时间没有明显变化（见第五节），可见瓶颈不在外观。
- 多 Agent + 严格发布流水线保证了稳定，但也意味着每个小改动成本很高（一个“小幅优化”要走 worktree、证据、门禁），客观上把产能导向了“修补和打磨”。

### Gaps
- 分类是按关键词自动粗分，个别 commit 可能归错类；没有统计代码行数或耗时，只统计 commit 个数。
- 300 个之前（6–9 月中）的投入结构未细分。

## 四、业务员的一天：各类信息怎么进 Trosa

### Takeaway
除了 Sela 外联邮件的回信/退信会自动进来以外，其他所有信息——WhatsApp 聊天、同事自己邮箱里的往来、报价、寄样、订单、付款——都要业务员手动录入；其中报价/样品/订单/付款连“该录在哪儿”的结构都没有，只能写成一句话或传个文件。

### Cited Findings
- **WhatsApp**：三条路，都要人主动：①“记录沟通”弹窗手写，沟通方式选 WhatsApp；②“沟通整理”把聊天文字或 WhatsApp 导出粘进来让 AI 整理（可选，需配置模型），或截图识别；③浏览器扩展在 WhatsApp Web 当前页面采集已加载消息，保存前确认，扩展需开发者模式安装。没有手机端入口。— [使用说明.md](使用说明.md)、[browser-extension/README.md](browser-extension/README.md)、[CHANGELOG.md 2026-10-19 “WhatsApp 导出能认出双方”](CHANGELOG.md)
- **邮件**：Trosa 自带的 Gmail 同步“当前未配置、已停用”；客户回信与退信由 Sela 读取邮箱后经 `/api/integrations/sela/reply` 交接——这只覆盖 Sela 发出的开发信。网易邮箱可用扩展手动采集。— [AGENTS.md](AGENTS.md)、[docs/SYSTEM_STATE.md §3](docs/SYSTEM_STATE.md)
  - 数据侧：近 30 天 Hamid 的沟通记录中来源 `sela_reply_engine` 70 条（其中退信 44 条）、手工 47 条、Agent 网关 6 条；Amy、Kelley、Emma 近 30 天 **100% 为手工**。Sela 外联邮件记录（outreach_emails）只存在于 Hamid 账号（2,933 条），其他三人为 0。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)
- **报价**：只能作为“沟通方式=报价”的一条文字记录，或在客户“文件”里上传报价单；没有报价对象（产品、规格、数量、单价、有效期、版本）。客户文件总数：Hamid 5 个，其余三人 0 个。— [app/static/index.html](app/static/index.html)、[只读库查询 2026-10-27](tools/trosa_maint_read.py)
- **样品**：只能写“沟通方式=寄样”的记录；无快递单号、寄出/签收/反馈状态。— 同上
- **订单 / PI / 付款 / 出货**：系统中没有对应对象；只有周报分类标签“订单签约”。— [app/static/index.html 第 969 行](app/static/index.html)、[migrations/](migrations/)
- **下一步与提醒**：只有在记录沟通时顺手填“下一步做什么 + 日期”，或在客户工作区“待办”分区新建；到期出现在“今天”。AI 预填的下一步带“AI 建议”标记，需人确认。— [PRODUCT_DESIGN_STANDARD.md §7](PRODUCT_DESIGN_STANDARD.md)、git log `[complete-modal-polish-0930]` U-11
- **新客户**：手动添加、批量添加、导入 Excel，或输入官网“从网站识别”（读官网事实与公开联系方式，确认后才填表）。Sela 开发的潜在客户自动进入（显示为“自动开发”/“Google / 官网搜索”来源）。— [使用说明.md](使用说明.md)、git log `[customer-source-google]`
- 历史沟通类型里大量“报价/寄样/会议”来自早期 Excel 迁移：90 天窗口（主要是 9-03 迁移批次）中 Amy 有 报价 197、寄样 170、会议 231；Kelley 报价 207、寄样 175、会议 155；而近 30 天三位同事的新记录几乎全是“follow_up/其他跟进/邮件/WhatsApp”，没有一条标成报价或寄样。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)

### Inferences
- 录入链路对“老板 + Sela”是半自动的，对同事是全手工的。这直接解释了为什么同事只在周会前集中补录。
- 迁移前的 Excel 里同事曾经大量记录“报价/寄样/会议”，说明这些是他们真实关心的节点；进入 Trosa 后反而不再这么标，可能是新弹窗默认选项和“沟通方式”的说法没让他们把报价/寄样当成一类事情来记。
- 最大的空白是：手机上的 WhatsApp → Trosa，以及“报价—样品—订单—收款”这条线的结构化。

### Gaps
- 不知道同事平时用的是哪个邮箱（Gmail/网易/企业邮），也不知道他们的 WhatsApp 主要在手机还是电脑上——这决定捕获方案。
- 扩展的实际安装与使用量查不到（沟通记录来源里没有单独的“扩展”标记；Inbox 里有 68 条 `gmail_capture` 类条目，来源是扩展还是旧 Gmail 同步无法确认）。

## 五、真实使用数据（只读汇总，不含任何客户内容）

### Takeaway
Trosa 每天在用的基本只有 Hamid（近 30 天 26 天有操作）。Amy、Kelley、Emma 近 30 天分别只有 10、6、6 天有操作，录入高度集中在周一、且大部分勾进周报；他们几乎不建待办、不写下一步、不用文件、不碰 Inbox。这印证了“开会时有用、平时不打开”。

### Cited Findings（全部来自 [只读库查询 2026-10-27](tools/trosa_maint_read.py)）
**活跃天数与录入频率（操作日志，9-14 起）**

| 成员 | 近30天有任何操作的天数 | 近30天有“记录沟通”的天数 | 近60天“记录沟通”次数 | 近60天“同步到周报”勾选次数 |
|---|---|---|---|---|
| Hamid | 26 | 22 | 182 | 41 |
| Amy | 10 | 10 | 58 | 33 |
| Kelley | 6 | 6 | 59 | 35 |
| Emma | 6 | 4 | 9 | 8 |

- 每周“记录沟通”次数（操作日志，周一起始）：Amy 9/14:9、9/21:9、10/05:3、10/12:22、10/19:2、10/26:13（9/28 那周 0）；Kelley 9/14:20、9/21:2、10/12:13、10/19:7、10/26:17（9/28、10/05 两周 0）；Emma 每周 0–4；Hamid 5–48。
- 星期分布（近 60 天“记录沟通”）：Kelley 周一 38 / 周二 9 / 周四 8 / 周日 4；Amy 周一 25 / 周三 21 / 其余 ≤5；Hamid 每天都有（周一 49、周五 37、周三 29…周日 19）。“同步到周报”勾选同样集中在周一（Amy 22/33、Kelley 22/35）。
- 9-10 以来手工记录：Hamid 64 条，录入延迟（录入日 − 发生日）中位数 0 天，28% 晚 ≥2 天，周报勾选率 47%，平均长度约 71 字；Amy 58 条，中位数 1 天，47% 晚 ≥2 天，勾选率 52%，约 50 字；Kelley 47 条，中位数 0 天，38% 晚 ≥2 天，勾选率 85%，约 54 字。（Emma 这一项查询时连接中断，未取到。）

**按沟通发生日（follow_date）的每周记录数（含 Sela 自动进入的部分）**
- Hamid：8/03 那周 168（Excel 历史）→ 9 月每周 11–14 → 10/12 43、10/19 48。
- Amy：8 月每周 11–25 → 9 月每周 8–9 → 10/05 5、10/12 20、10/19 10。
- Kelley：8 月每周 5–16 → 9 月每周 4–10 → 9/28、10/05 两周 0 → 10/12 13、10/19 11。
- Emma：总共 12 条（9 月起），客户 2 个。

**渠道/来源（近 30 天，按发生日）**
- Hamid 123 条：Sela 回信引擎 70（含退信 44、客户回复约 21）、手工 47、Agent 网关 6。
- Amy 38 条全手工（follow_up 30、其他跟进 8）；Kelley 28 条全手工（邮件 13、follow_up 10、WhatsApp 5）；Emma 6 条全手工。
- 方向字段在 90 天窗口里大部分为“unknown”（Amy 1573/1662、Kelley 1087/1149），多为迁移数据。

**待办（提醒）**
- 9-05 之后手动建的待办：Hamid 57（完成 41、逾期未完成 11、未来 5）；Amy 2（都在未来）；Kelley 0；Emma 0。
- 当前未完成的手动待办：Hamid 逾期 11、未来 14 天内 3、更远 3（涉及 17 个客户）；Amy 未来 2；Kelley 0；Emma 0。
- 完成待办的周数：Amy 最后一次完成在 9/14 那周；Kelley 最后一次在 8/17 那周；Hamid 10 月每周 4–11 次。
- 已冻结的“15/30/60 天自动开发节点”旧行仍躺在库里且未完成：Hamid 约 583 条、Amy 306 条、Kelley 299 条（正式界面不显示，属历史遗留）。

**客户覆盖**
- 客户总数（未删除）：Hamid 1,698（其中 224 标记来源“Google / 官网搜索”，即 Sela 开发）；Amy 182；Kelley 159；Emma 2。
- 近 30 天有任何沟通记录的客户数：Hamid 93、Amy 21、Kelley 10、Emma 3；近 90 天：Hamid 280、Amy 77、Kelley 22、Emma 5。
- 有过“客户主动来信（inbound）”记录的客户：Hamid 54（近 30 天 19）、Amy 9（3）、Kelley 10（4）、Emma 0。
- 客户文件：Hamid 5 个，其他人 0。

**Inbox**
- 旧式 Inbox 条目共 218 条（resolved 180、archived 38），多为 9 月中旬产生：`gmail_capture` 68、`sela_identity_review` 39、`sela_agent_request` 35、`sela_follow_up` 13、`ai_suggestion` 3；10 月后每周只有 3–8 条新增。处理方式：自动 88、空 75、人工 53（其中明确记录为 hamid 的 26）、sela 2。
- 新的 Inbox 对话：共 308 个线程（closed 307、open 1），按周新开：10/05 19、10/12 184、10/19 99、10/26 6；关闭者 sela 278、人工 29。消息 459 条：sela 410、**人类 49 条全部来自 hamid**。

**操作日志总量（近 30 天）**：7,756 条，其中 Hamid 的 UPSERT 6,509 条（主要是 Sela 批量写线索）、Agent 网关写入 296 条；同事三人合计约 200 条。

### Inferences
- “每个同事每天都想打开”目前离得很远：同事是**每周一次、为周会补录**的使用模式；周报是他们唯一的稳定动机。
- 同事从不挂待办 → 今天页对他们是空的 → 没有每日回访的理由。这是一个“鸡和蛋”：要么让系统自动给出“今天该跟进谁”（例如基于客户来信未回、报价后 N 天未回、样品寄出后到期等），要么降低挂待办的成本到几乎为零。
- Hamid 的高活跃有相当部分来自 Sela 的自动流量（回信/退信/对话），不能直接当作“产品对人有吸引力”的证据。
- Emma 账号 9 月中开通，但只有 2 个客户、12 条记录，像是“开了号但没迁入工作”，说明新成员上手（导入自己的客户、知道怎么用）缺一条路。

### Gaps
- 操作日志从 9-14 才开始，看不到更早的使用节奏；8 月的数据混有 Excel 迁移。
- 无法区分“打开了页面但没写东西”的访问（没有页面访问日志），所以“活跃天数”只代表“有写入的天数”，真实打开频率可能略高。
- Emma 的录入延迟与“客户有开放待办数”两项查询因 SSH 隧道中断未取到。
- 联系人数量统计（兼容视图 `trade_os_compat.contacts`）结果异常（Kelley 只有 7），口径不可靠，未采用。

## 六、冻结/移除的功能，以及它们说明了什么

### Takeaway
被冻结的几乎都是“系统自己推导/自己生成东西塞给人”的功能（网站监控、AI 客户研究与推荐、自动 15/30/60 天开发节点、独立 AI 聊天前端、Apple 日历订阅）。教训被总结成了“系统别替人做判断”，这条本身没错，但被推广成了“系统别主动做事”，于是把“自动帮人省录入、自动提示该跟进谁”这种低风险高价值的自动化也一并挡在了门外。

### Cited Findings
- **网站监控、客户 AI 研究/推荐**：迁移 0019 删除兼容视图、基表保留审计、正式运行面冻结；原因：“推理型内容占据核心页面、制造无依据行动；前台工作负担过重。”“不得恢复，除非用户明确要求。”— [docs/CHANGELOG.md](docs/CHANGELOG.md)；[TROSA_MAINTENANCE.md「已冻结」](TROSA_MAINTENANCE.md)
- **15/30/60 天自动开发节点**：历史行仅保留供审计，正式运行不再创建/展示/操作；`trosa_domain.py` 用 SQL 强制“退役 outreach 调度行永不能转为待办”，原因是“历史自动开发节点污染 Today”。— [docs/CHANGELOG.md](docs/CHANGELOG.md)、[TROSA_MAINTENANCE.md](TROSA_MAINTENANCE.md)；库里仍有约 1,190 条此类未完成旧行（见第五节）— [只读库查询 2026-10-27](tools/trosa_maint_read.py)
- **Pi 前端聊天运行时**：2026-08-31 “add Hamid Pi chat runtime”，9-01 “bind Pi assistant to Hamid CRM workspace”，9-07 多个部署修复，9-28 清理文档残留；现结论“不恢复独立 Pi 前端运行时；保留的 `/api/agent/*` 只能做原子读取和确认式提案”。— git log；[AGENTS.md](AGENTS.md)
- **Apple 日历 ICS 订阅**：2026-10-01 移除。— git log `[remove-apple-calendar]`；[AGENTS.md](AGENTS.md)
- **Trosa 自带 Gmail 同步**：未配置、已停用，改由 Sela 读邮箱交接。— [AGENTS.md](AGENTS.md)
- **Sela 对已有客户的跟进接口**：2026-09-19 退役（`[retire-sela-follow-up]`），Sela 只做 prospect 开发。— git log
- **旧式等级/阶段/状态/标签/批量维护**：降为次级、列为“未来删除候选”，先隐藏入口、观察 60–90 天使用再决定。— [TROSA_MAINTENANCE.md「功能处置」](TROSA_MAINTENANCE.md)
- **设计层面**：Daylight 规范、房间文档、探索原型、灵感库于 10-13 删除；“灯箱/潮汐”等房间方向被瑞士式默认外观取代。— [design/DESIGN_WORLD.md](design/DESIGN_WORLD.md)、git log `[retire-old-design-1013]` `[direction-superseded-1013]`

### Inferences（教训）
- **自动生成的“该做的事”如果没人认领，就会变成没人看的债务**：15/30/60 节点留下上千条逾期行（官方理由是它们“污染 Today”；同事当时是否因此不再看今天页，无法证实）。说明“提醒”必须来自人确认过的承诺或真实信号（客户来信未回、报价无回音），不能来自机械日程。
- **AI 推理内容放在核心页面 = 噪音**：研究/推荐、网站监控被砍，是因为它们“说得多、做不了”。但反过来，AI 用在“把聊天变成记录、给出可一键确认的下一步”是**减负**而不是加负，不应和前者同等对待。
- **单独的聊天前端不是工作发生的地方**：Pi 聊天绑定在 Hamid 一人、部署复杂、很快退役。AI 应该嵌在“记录沟通”“今天”这些已有动作里，而不是另开一个窗口。
- **只为一人服务的能力很难活下来**：Pi、Gmail 同步、Apple 日历都只对少数人有用。当前的 Sela/Inbox 对话也有同样风险（人类使用者只有 Hamid）。

### Gaps
- 没有找到当时冻结网站监控/AI 研究时的具体使用量数据，只有文字理由。
- 不清楚 15/30/60 节点在同事账号里存在期间，同事是否因此放弃了“今天”页（时间上吻合：Amy、Kelley 的待办完成记录在 8 月底—9 月中断档，但无法证明因果）。
