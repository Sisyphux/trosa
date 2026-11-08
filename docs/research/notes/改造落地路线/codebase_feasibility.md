# Trosa 改造落地路线：在现有代码里能不能做、怎么做、多大

> 调研日期 2026-10-27。严格只读：读了代码、迁移、文档，并用维护者只读通道 `tools/trosa_maint_read.py`（只读角色，`--as-user` 按成员视角）查了**汇总数字**，没有抄录任何客户名、邮箱、电话或沟通原文；没有读 `workbench.env`，没有任何写入、提交或发布。
> 来源写法：仓库文件用相对仓库根目录的路径（根 = `/Users/luoxin/Desktop/Trosa/.claude/worktrees/undesigned-pages-audit-f0e651/`），带行号的写成 `文件:行号`；数据库数字统一标为 [只读库查询 2026-10-27](tools/trosa_maint_read.py)。
> 工作量单位：S ≈ 1 个专注 Agent 日以内，M ≈ 2–3 天，L ≈ 4 天以上；都不含等人放行发布的时间。都是粗略推断。

## 一、功能 A（最优先）：AI 每天扫一遍"今天有没有要跟进的" + 今天页建议 + 嵌在今天/客户页里的"问问这个客户"

### Takeaway
能做，而且一半零件已经在：定时器（APScheduler，跑在正式服务进程里）、共享模型调用（`ask_llm`/`quick_chat`，6 家服务商、有超时和预算）、只读的客户事实接口（`/api/agent/brief/today`、`workspace`、`timeline`、`messages/search`）、"提议 → 人确认 → 才写入"的整条链（`audit.agent_proposals` + 前端确认弹窗）、以及判断"这个客户归人管还是归 Sela 管"的内容规则。真正要新建的是：①一套确定性的"候选规则"（谁该跟），②一个存"建议"的地方（建议 ≠ 待办），③今天页上的建议区和"问一句"输入框，④一个只读问答接口。最大的风险不是技术，而是**数据本身不够准**：同事手工记录的"方向"有 40–50% 是 unknown，"我们回了没有"在系统里很多时候判断不出来——所以第一版规则必须以"报价/寄样后无回音""老客户久未联系""客户亲口说过的日期"为主，"客户来信未回"只对有可靠方向的记录（主要是 Sela 交接进来的客户回信）成立。整体约 9–13 个 Agent 日，其中一步（新表）是需要人工放行的 T2 迁移。

### Cited Findings

**定时任务怎么跑（可复用）**
- 调度器是 APScheduler `BackgroundScheduler`，时区固定 `Asia/Shanghai`，`coalesce=True, max_instances=1, misfire_grace_time=3600`；现有任务只有三类：每天 02:15 本机快照、可选的邮箱 SMTP 验证（按间隔）、可选的 Gmail 同步（按间隔）。— [scheduler.py](scheduler.py)
- 调度器在正式入口 `serve.py` 里、Waitress 启动前启动，与网页请求同一进程（默认 8 线程 `CRM_THREADS`）。— [serve.py:61、68](serve.py)
- 按成员跑任务的现成写法：`for user in USERS:` → `_user_module_enabled(user, 模块)` 检查该成员是否关了模块 → `set_db_user(user)` → 干活 → 最后 `set_db_user(None)`；单个成员失败只记日志不影响别人。— [scheduler.py `_run_email_verification_jobs`](scheduler.py)
- 必须按成员切换的原因：PostgreSQL 的读模型视图（如 `trosa.customer_interactions`）用 `trosa.compat_current_user()` 过滤到"当前成员"的行。— [migrations/0034_customer_history_binding.sql](migrations/0034_customer_history_binding.sql)
- 成员可关的可选模块里已有 `ai_assistant`（另有 `email_validation`、`weekly_overview`、`outreach` 等），可直接作为"每日建议"的开关。— [app.py:974](app.py)
- 每条 SQL 默认 60 秒语句超时（`TRADE_OS_STATEMENT_TIMEOUT_MS`）。— [postgres_compat.py:27](postgres_compat.py)

**模型接入现状（可复用）**
- 一个进程只解析出**一套共享连接**：`deepseek`（默认模型 `deepseek-chat`）、`qwen`（`qwen-plus`）、`glm`（`glm-4-flash`）、`openai`/兼容接口（`gpt-4o-mini`）、`lmstudio`、`ollama`；`auto` 模式按 DeepSeek→通义→GLM→OpenAI→本地 的顺序取第一个有密钥的，所有 AI 功能共用。— [app/engine.py:271–351](app/engine.py)
- 密钥：设置页写到私有文件 `ai-config.env`（权限 600），正式环境在 `/var/lib/trade-os/ai-config.env`，**不进 PostgreSQL**；也可用环境变量。只有管理员（或内置 hamid）能改和测试。— [app/engine.py:195–210、405–426](app/engine.py)；[DEPLOYMENT.md:124](DEPLOYMENT.md)；[app.py:2834–2900](app.py)
- 调用参数：`temperature 0.3`，`max_tokens` 2048–3072；单次超时 DeepSeek/通义/GLM 180 秒、OpenAI 60 秒、读图 120 秒；返回纯文本，出错时返回以 `[错误]` 或 `[ERROR_` 开头的字符串（不抛异常）。没有 JSON 模式，调用方自己截取第一个 `{…}` 解析。— [app/engine.py:692–912](app/engine.py)；[app.py:14966–14975](app.py)
- 网页请求共用 60 秒外呼预算（`WEB_OUTBOUND_BUDGET_SECONDS = 60`，`_with_outbound_budget`），为的是不超过 Cloudflare 约 100 秒的 524 上限；**后台任务不开预算，用各自更长的超时**。— [app.py:14835–14846](app.py)；[app/engine.py:27–62](app/engine.py)；524 事故复盘见 [docs/repro/ai-budget-524/README.md](docs/repro/ai-budget-524/README.md)
- 已有"只按资料回答"的问答外壳 `quick_chat(question, customer_context)`：提示词要求"只使用提供的 CRM 资料……资料不足时直接说明 CRM 中未找到该信息"，不许推荐报价/交期/策略。— [app/engine.py:978–992](app/engine.py)
- 在 `app/engine.py` 里没有找到任何 token 用量/费用记录（grep `usage`、`prompt_tokens`、`total_tokens` 无结果）。— [app/engine.py](app/engine.py)

**客户事实与"问一句"可复用的只读接口**
- `/api/agent/brief/today`：今天及逾期待办 + 打开的 Inbox（≤20）+ 未来 7 天待办，纯规则，不调模型；可出 JSON 或 Markdown。— [app.py:16438–16529](app.py)
- `/api/agent/customers/<id>/workspace`：客户资料、联系人、未完成待办、最近 20 条往来、`information_gaps`（"未记录联系人/尚无明确下一步"等）以及 `write_policy: 请先创建提议；待用户确认后才写入 CRM`。— [app.py:16532–16572](app.py)
- `/api/agent/customers/<id>/timeline`：最多 100 条事件，每条带 `event_date / activity_type / direction / content / result / next_plan / source`，并声明"空字段表示系统没有记录，不代表事情没有发生"。— [app.py:16575–16616](app.py)
- `/api/agent/messages/search`：跨客户按关键词、国家、方向、回复状态、日期搜索记录与外联邮件。— [app.py:16619 起](app.py)
- 这些接口都是 `@login_or_gateway_read`：网页登录和 Agent 令牌都能读；CLI 有对应命令 `brief / workspace / timeline / search / propose-task / propose-communication`。— [app.py:16439](app.py)；[tools/trosa_cli.py:312–480](tools/trosa_cli.py)
- AGENTS.md 的边界：不恢复独立 Pi 前端；保留的 `/api/agent/*` "只能做原子读取和确认式提案"；无人值守的后台自动化"不得自动创建客户、联系人、待办或商业承诺"。— [AGENTS.md](AGENTS.md)

**"建议 → 人确认 → 才变成带日期的待办"可复用的链路**
- 表 `audit.agent_proposals`：`proposal_type`、`payload jsonb`、`proposal_action`、`source`、`source_reference`、`idempotency_key`、`status`（默认 `pending`）、`confirmed_at`。— [migrations/0001_unified_trade_os.sql:618–631](migrations/0001_unified_trade_os.sql)
- 接口：`POST /api/agent/proposals`（类型只允许 `task`/`activity`；**task 提议必须同时有动作和 `due_date`**）、`GET/PUT /api/agent/proposals/<id>`、`/confirm`、`/cancel`；Agent 令牌走 `/api/gateway/proposals`，要求 `Idempotency-Key`。— [app.py:17668–17830](app.py)；[app.py:17231–17271](app.py)
- 前端已有 `openAgentProposalConfirmation(proposalId)`：读提议，打开预填好的"记录沟通/新建待办"弹窗，人改完再确认。— [app/static/app.js:3518–3540](app/static/app.js)
- 记录沟通的统一写入事务 `record_customer_communication`：同一事务里写记录、处理下一步待办、关闭 Inbox 项，返回 `undo_token`。— [app.py:18604 起](app.py)

**"该跟谁"的候选信号，代码里现成能算的**
- `trosa_domain.customer_facts`：每个客户最近一次沟通、最近一次外联邮件、最早一条未完成待办；`waiting_reply` **只来自外联邮件的投递状态**（`pending`/`no_reply`），手工记录不产生"等待回复"；`contact_state` 只有 contacted/uncontacted 两值。— [trosa_domain.py:1896–2062](trosa_domain.py)
- `trosa_domain._relationship_bucket` / `human_owned_customer_ids`：按**内容**（正则 `_RE_QUOTE`、`_RE_SAMPLE_CUST`、`_RE_STRONG_ORDER`、`_RE_MEET`、`_RE_REJECT` 等）把客户分成 engaged / human_reminder / needs_info / auto_follow；auto_follow（纯单向开发）归 Sela，不进今天页。今天页已用它过滤。— [trosa_domain.py:120–312](trosa_domain.py)；[migrations/0059_today_content_relationship_boundary.sql](migrations/0059_today_content_relationship_boundary.sql)
- 客户账页已有"沉默"判断：`days_since_contact >= silent_threshold`，阈值按客户等级（A/B/C+ 用一个天数，其余用另一个）。— [app.py:10735、11249–11251](app.py)
- AI 整理提示词已能抽出 `needs`（客户提出且我方未答复的问题）、`concerns`、`intent`、`suggested_next_action`（明确规定"不要写日期"）、`message_date`、`direction`。— [app.py:14848–14876](app.py)
- 今天页读取 `trosa.today_tasks`，同客户同日只显示一条，并且排除 `outreach_%` 类型的旧自动节点。— [trosa_domain.py:314–365](trosa_domain.py)

**存建议可借用的现成结构**
- `trosa.inbox_items` 有 `dedupe_key`（非空唯一索引）、`status`、`snoozed_until`、`resolved_at`、`resolution_reason`。— [migrations/0001_unified_trade_os.sql:239–256](migrations/0001_unified_trade_os.sql)
- `trosa.tasks` 有 `task_type`、`status`、`legacy_payload jsonb`、`manual_order`。— [migrations/0001_unified_trade_os.sql:185–201](migrations/0001_unified_trade_os.sql)
- Inbox 对话模块 `inbox_dialogue.py` 是确定性的服务端模块，自带幂等回执表、每日新线程上限 `DAILY_NEW_THREAD_CAP = 200`，并明确写着"No model calls"。— [inbox_dialogue.py:1–45](inbox_dialogue.py)

**15/30/60 旧节点的教训在代码里的样子**
- 旧"自动开发节点"是带日期的待办（`task_type` 以 `outreach_` 开头），所有今天页/事实查询都写死 `NOT LIKE 'outreach_%'` 排除；后来又用迁移 0039/0040 把"开发新客户/二次开发"类旧待办批量标为完成。— [trosa_domain.py:338–342](trosa_domain.py)；[migrations/0039_autoclose_routine_development_tasks.sql](migrations/0039_autoclose_routine_development_tasks.sql)
- 现状调研记录库里仍有约 1,190 条此类未完成旧行（Hamid 583、Amy 306、Kelley 299）。— [research_notes/Trosa 产品方向深度调研/trosa_current_state.md 第五节](../产品方向深度调研/trosa_current_state.md)

**今天页前端（要改的地方）**
- 今天页 = 潮线图 + 客户行（`#todayReminders`）+ 选中客户上下文（`#todayWideDetail`）+ 焦点卡（`#todayFocus`，空时文案"今天没有到期的下一步"）+ 右侧 14 天日程；数据来自 `/api/reminders/today` 与 `/api/reminders/upcoming`。— [app/static/index.html:173–240](app/static/index.html)；[app/static/app.js:4394 起（潮汐）、4810、5444 `renderTodayFocus`、5598 `renderTodayWideDetail`](app/static/app.js)

**真实数据：候选信号够不够、准不准**（全部来自 [只读库查询 2026-10-27](tools/trosa_maint_read.py)，窗口为近 90 天有沟通的客户，看"最近一条沟通"的方向）
- Hamid：最近一条是 outbound 195 个客户、unknown 36、inbound 35（其中 31 个已超过 2 天）、two_way 14。
- Amy：unknown 39、outbound 28、two_way 9、inbound 1。
- Kelley：unknown 9、outbound 8、inbound 3、two_way 2。Emma：unknown 3、outbound 1、two_way 1。
- 近 30 天新记录的方向：Hamid 的 Sela 回信引擎 outbound 49 / inbound 21，手工 outbound 21 / two_way 16 / unknown 9；Amy 手工 outbound 12 / two_way 12 / unknown 11 / inbound 3；Kelley 手工 unknown 15 / inbound 5 / two_way 5 / outbound 3。
- 最近一条沟通在 7–120 天前、且内容/类型命中报价或寄样关键词的客户（之后再无沟通）：Hamid 报价 18、寄样 6；Amy 报价 6、寄样 5；Kelley 报价 8、寄样 3；Emma 0。
- "历史上出现过下单/订单/定金/尾款/出货/order/PO/deposit/shipment 字样、但 90 天内没有任何沟通"的客户：Kelley 19、Amy 5、Hamid 0、Emma 0。
- 近 60 天手工记录里提到"下周/下个月/月底/明天/X月X日/next week/具体日期"之类未来时间的：Amy 58 条中 4 条、Kelley 47 条中 1 条、Emma 9 条中 0 条（Hamid 这条查询两次都因连接被服务端断开没取到）。
- 近 30 天手工记录填了"下一步"的：Hamid 47 条中 7、Amy 38 中 1、Kelley 28 中 0、Emma 6 中 0。

### Inferences
- **候选怎么算（确定性，先规则后模型）**。建议第一版只用四条规则，且全部从时间线算、不信 `contact_state`/`next_task_date`：
  1. **客户说了话我们还没回**：最近一条记录 `direction='inbound'` 或 `activity_type='customer_reply'`（Sela 交接的回信天然满足），且已过 1–2 个工作日。按上面的数字，这条对 Hamid 有 ~30 个客户，对同事几乎为 0——因为同事是事后补录、一条记录里写双方，`two_way`/`unknown` 判断不出谁最后说话。
  2. **报价/寄样/PI 之后没下文**：最近一条记录是报价/寄样/PI 类（`activity_type` 或现成 `_RE_QUOTE`/`_RE_SAMPLE_CUST` 正则），之后 N 天（建议报价 7 天、寄样 10–14 天）再无记录。三位同事合计 ~22 个客户、Hamid ~24 个，这是对同事最有内容的一条。
  3. **成交过但久未联系的老客户**：出现过订单类字样、90 天无沟通。Kelley 19、Amy 5，每周只放 2–3 个，避免一次涌出。
  4. **客户亲口说过的日期**：记录里出现的未来日期——当前数据里很少（每人每月 0–4 条），所以这条要靠以后"记录时 AI 抽日期"（功能 B）才会多起来，第一版可以只做抽取不做依赖。
  然后全部过一遍 `human_owned_customer_ids`（把 Sela 的纯开发客户排除）和"已有未完成待办的客户不重复建议"。
- **模型只做两件事：排序 + 一句理由**。每天每个成员把 ≤20 个候选连同每人最近 5–10 条时间线摘要一次性喂给模型，让它挑出 3–5 个并各写一句"为什么今天该跟、上次说到哪"。模型失败时直接按规则顺序展示、理由用规则文案（"报价已 9 天无回音"），这样未配置 AI 时今天页仍完整（AGENTS.md 硬要求）。后台跑不受 60 秒网页预算限制，但要自己设总时限，单成员失败不影响别人。
- **存哪里：建议新建一张小表，而不是复用待办或 Inbox**。理由：①复用 `trosa.tasks` 等于重走 15/30/60 的路（带日期、会逾期、会堆积）；②复用 `audit.agent_proposals` 不行，它要求 task 提议必须有日期，而"建议"的本质是"还不知道哪天"；③复用 `trosa.inbox_items` 会和 Sela 的 Inbox 混在一起，且同事几乎不进 Inbox。表的形状可以很简单：成员、客户、日期（哪天生成的）、规则编号、证据引用（哪条记录）、理由、状态（新/已处理/不用跟/稍后）、`snooze_until`、`dedupe_key = 成员+客户+规则+证据记录`。这一步是 `migrations/` 改动，属于需要人工放行的 T2（见第七节）。**不想先动迁移的验证版**：每天按需现算（请求时只跑规则、不调模型），"不用跟/稍后"先存在成员偏好 `app_settings` 的 JSON 里——能上线给同事试，但不宜长期用。
- **怎么避免变成第二个 1,190 条**：建议**每天重算、不累积**——昨天没处理的建议今天要么因为规则仍成立而再出现（同一 `dedupe_key`，不新增行），要么规则不成立就自然消失；从不产生"逾期"；每人每天上限 5 条；"不用跟"对同一证据永久静音、"稍后"静音到指定日；只有人点"安排"才调用现有提议/待办写入，变成带日期的待办。
- **"问问这个客户"**：做成今天页焦点卡和客户工作区里的一个输入框（不是独立聊天窗口），后端新开一个只读接口：用户问题 + 指定客户的 `workspace` + `timeline`（≤50 条）→ `quick_chat` → 返回答案 + "建议安排的下一步（可选）"按钮，按钮走现有 `openAgentProposalConfirmation` 那套预填弹窗。问题里只提客户名时，先用现有客户搜索（含模糊/同义词/拼音）定位，匹配不唯一就让人点选，不让模型猜。网页请求受 60 秒预算约束，长时间线需要截断。
- **工作量拆分（粗估）**：A1 规则候选 + 单元测试 M（2–3 天）；A2 建议表 + 处理/稍后/安排接口 M（2 天，含 T2 迁移）；A3 后台每日任务 + 模型排序理由 + 失败兜底 S–M（1–2 天）；A4 今天页建议区 UI + 真实浏览器验收（宽屏/iPad/iPhone）M（2–3 天）；A5 "问一句"接口 + 嵌入两处 UI M（2–3 天）。合计约 9–13 天。A1 可以完全不碰前端和迁移，先产出"影子名单"给老板看。
- **主要风险**：①方向字段不可靠导致"未回复"误报/漏报（同事那侧尤甚）；②调度器与网页同进程，模型调用若用同步长超时要放在独立线程池，避免占满 8 个 Waitress 线程；③多台进程时会重复跑（目前单 ECS 单进程，`max_instances=1` 足够，但以后若开多进程需加锁或去重键）；④隐私：每日任务会把同事的客户对话摘要发给共享模型服务商（DeepSeek 等境外/境内服务），需老板拍板；⑤费用：每人每天 1 次排序 + 少量问答，量级很小，但当前没有用量记录，无法事后核算；⑥Hamid 名下数据量大（客户 1,698、外联邮件 2,933），重型正则扫描全量时间线会慢（本次只读查询在 Hamid 视角两次被断开），候选计算要先用日期窗口和索引缩小范围。

### Gaps
- 正式环境当前到底配了哪家模型、是否可用：没读配置文件（按约束），无法确认；周报中文翻译、AI 整理等功能存在只说明"曾被使用"。
- 每日任务应该在北京时间几点跑（同事上班前 8:30？还是按客户时区分批）需要老板定。
- 未测量 `trosa.customer_interactions` 视图在 Hamid 全量上的实际耗时；连接被断开可能是 SSH 隧道问题，也可能是查询过重，未能区分。
- "已有未完成待办就不建议"是否合理（Hamid 有 11 条逾期待办）需要产品判断。

## 二、功能 B：10 秒录入（粘贴/转发/截图/WhatsApp 导出 → 默认 AI 整理 → 一键确认），包括手机浏览器

### Takeaway
"把原文变成一条记录"的后端几乎都在：AI 整理接口、截图识别、WhatsApp 导出按发言人分方向、原文入库前自动摘要、统一确认弹窗、撤销。差距在**入口和默认路径**：AI 整理现在藏在"AI 帮我整理"按钮后面、要先选客户再打开弹窗、整理出的下一步不带日期；手机上没有"分享到 Trosa"的入口（没有 Web App Manifest/分享目标）。补齐约 M–L（4–6 天），多数是前端改动（T1，无需迁移）。

### Cited Findings
- AI 整理接口 `POST /api/inbox/analyze-reply`：原文 ≤12,000 字，按现有客户/联系人的公司、名称、邮箱、电话、网站做文字匹配，给出最多 5 个候选客户和分数；没配 AI 时退回"截取前 220 字"作摘要并标 `ai_available: False`。— [app.py:14878–14990](app.py)
- 截图识别 `POST /api/inbox/extract-image`（模型读图，超时 120 秒）。— [app.py:14995](app.py)；[app/engine.py:715–770](app/engine.py)
- 原文入库闸门：`record_customer_communication` 检测到内容像原始邮件/聊天（`looks_like_raw_message`）就先调用 `_summarize_raw_communication` 摘要，失败退回确定性一句话事实；原文本身不写进时间线。— [app.py:18575–18625](app.py)；[communication_text.py:349–395](communication_text.py)
- 记录弹窗：沟通方式下拉 + "这次发生了什么"富文本框（占位符"可直接粘贴完整邮件、WhatsApp 对话或会议内容"）+ **"AI 帮我整理"按钮**（需手点）+ 方向自动识别 + 下一步与日期 + "加入本周工作"。— [app/static/index.html:781–790](app/static/index.html)；[app/static/app.js:3934–3965 `analyzeCommunication`](app/static/app.js)
- 统一确认弹窗 `openCommunicationConfirm(options)`，今天/Inbox/客户页共用；保存后返回撤销令牌。— [app/static/app.js:3541 起](app/static/app.js)；[app.py:16420–16425](app.py)
- AI 提示词里 `suggested_next_action` 明确"不要写日期"，`message_date` 只取沟通发生日。— [app.py:14868–14872](app.py)
- 浏览器扩展：MV3 侧边栏，需开发者模式加载；只采集网易邮箱当前线程和 WhatsApp Web 已加载消息；调用 `/api/extension/match`、`/api/inbox/analyze-reply`、`/api/extension/communications`、`/api/undo/`。— [browser-extension/README.md](browser-extension/README.md)；[browser-extension/sidepanel.js](browser-extension/sidepanel.js)
- 移动端：`swiss-d.css` 有 21 处 `@media`，主断点 820px；`index.html` 里没有 `manifest`、`share_target` 或 `apple-mobile-web-app` 相关标签（grep 无结果）。— [app/static/swiss-d.css](app/static/swiss-d.css)；[app/static/index.html](app/static/index.html)
- 同事近 30 天记录 100% 手工；Kelley 标成 WhatsApp 的只有 5 条。— [trosa_current_state.md 第四、五节](../产品方向深度调研/trosa_current_state.md)

### Inferences
- 最小改法（S–M，约 2 天）：粘贴超过一定长度时**自动**触发整理（去掉按钮这一步），整理完直接预填"发生了什么/方向/客户候选"，人只点"保存"；提示词增加"客户亲口说的日期"和"这次涉及报价/寄样/PI…"两个字段（同时喂给功能 A 和 C）。
- 一个"随手记"入口（M，约 2 天）：今天页和客户列表顶部一个粘贴框/上传截图，不用先选客户，靠现有候选匹配；匹配不唯一时让人点选（AGENTS.md 规定只有唯一精确邮箱/手机号才能自动确认）。
- 手机（M，约 1–2 天）：加 Web App Manifest + `share_target`，让安卓"分享"菜单能把 WhatsApp 文字/导出发进 Trosa；iOS Safari 不支持 `share_target`，只能靠"复制 → 打开 Trosa → 粘贴"，需在真实 iPhone 宽度验收。需要新增静态文件和一个接收路由。
- 风险：每次粘贴都调模型，费用与等待（网页 60 秒预算内）；长 WhatsApp 导出要截断到 12,000 字；原文含客户隐私发给模型服务商（与 A 同一个决策）。

### Gaps
- 不知道同事的 WhatsApp 主要在手机还是电脑、手机是安卓还是 iPhone——决定 `share_target` 值不值得做。
- 没在真实手机浏览器里走过现有记录弹窗，未验证 820px 以下是否好用。

## 三、功能 C：把"事"（报价/寄样/PI/定金/出货/尾款）和"渠道"分开

### Takeaway
现在"渠道"和"事"挤在同一个字段 `event_type`（界面叫"沟通方式"）里，而且库里的取值是中英混杂的（`follow_up`、`其他跟进`、`邮件`、`WhatsApp`、`customer_reply`、`客户回复`、`周内汇总`…）。最省事的落地是：渠道继续用 `event_type`，"事"作为多选标记存进已有的 `timeline_events.payload jsonb`——写入不需要改表；但要让今天页规则、周报、客户页高效读到它，需要改读模型视图（迁移，T2）。历史数据可以用现成的报价/寄样正则做一次"建议标记"回填。约 M（3–4 天）。

### Cited Findings
- 记录弹窗"沟通方式"选项：WhatsApp、邮件、电话、会议、报价、寄样、展会现场、微信、视频会议、客户来访、其他跟进（渠道与事件混在一起，没有 PI/定金/出货/尾款）。— [app/static/index.html:781](app/static/index.html)
- 后端标签表 `_LEDGER_ACTIVITY_LABELS` 用英文代码（`whatsapp/email/phone/meeting/quote/sample/follow_up/customer_reply/task_completed`…）。— [app.py:11214–11216](app.py)
- Excel 导入用关键词把历史记录归为 `quote/sample/meeting/email/phone/follow_up`。— [app.py:21341–21353 `_activity_type_from_excel`](app.py)
- 近 30 天真实取值：Hamid `follow_up` 44、`outreach_bounced` 44、`customer_reply` 15、`其他跟进` 11、`客户回复` 6、`周内汇总` 1、`外联退信` 1；Amy `follow_up` 30、`其他跟进` 8；Kelley `邮件` 13、`follow_up` 10、`WhatsApp` 5；Emma `follow_up` 4、`其他跟进` 2。近 30 天三位同事没有一条标成报价或寄样。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)
- `timeline_events.payload jsonb` 已存在（现在放 `is_reported`、`is_deleted` 等）；读模型视图只取出 `is_reported`，没有通用 payload 列。— [migrations/0001_unified_trade_os.sql:203–217](migrations/0001_unified_trade_os.sql)；[migrations/0034_customer_history_binding.sql](migrations/0034_customer_history_binding.sql)
- 现成的内容正则 `_RE_QUOTE`、`_RE_SAMPLE_CUST`、`_RE_STRONG_ORDER` 等可识别报价/寄样/订单。— [trosa_domain.py:120–230](trosa_domain.py)

### Inferences
- 新"事件标记"建议存为 `payload.deal_events = ['quote','sample',…]`，渠道值同时做一次规范化（中英混杂的值统一映射为固定代码，界面显示中文）。规范化可以只在读时映射，不改历史行，避免大批量更新。
- 回填：用正则给历史记录打"可能是报价/寄样"的**建议**标记（不当事实），避免把 1,000+ 条迁移数据里的"报价"(Amy 197、Kelley 207) 当成近期报价。
- 改视图暴露 `deal_events` 是 T2 迁移；在 A 的建议表迁移里一起做可以只走一次人工放行。
- 风险：改 `event_type` 的选项会影响今天页、周报、客户账页的标签显示和 Sela 交接写入的类型（`customer_reply` 等），必须保持旧值可读。

### Gaps
- 未确认 Sela 交接、扩展采集、Agent 网关写入时各自使用的 `event_type` 全集。

## 四、功能 D：周报从日常记录自动汇总（从"勾选加入"改为"勾掉排除"）

### Takeaway
周报只是一个读视图（`is_reported=true` 的记录按公司聚合），改成"默认全进、勾掉排除"技术上是 S–M（1–2 天）：要么在写入时默认 `is_reported=true`，要么把查询改成"本周全部手工记录减去被排除的"。关键坑：Hamid 名下 Sela 自动进来的大量记录（近 30 天回信引擎 70 条，其中退信 44 条）必须按来源排除，否则周报会被淹没。另外发现：完成待办弹窗里的"周报分类"（重点跟进/新客户/报价进展/样品寄送/订单签约）**根本没有保存**，只是改按钮颜色。

### Cited Findings
- `weekly_interactions` 只取 `is_reported=true`、排除 `outreach_bounced`、排除已删客户。— [trosa_domain.py:466–508](trosa_domain.py)；[migrations/0118_bounces_not_weekly_work.sql](migrations/0118_bounces_not_weekly_work.sql)
- `_build_weekly_summary` 按规范化公司名聚合，输出 `actual_work / result / next_step`，带后台刷新缓存；不按分类分组。— [app.py:22337–22468](app.py)
- 前端记录时默认 `is_reported: 0/false`。— [app/static/app.js:4172、4191、5714](app/static/app.js)
- 周报分类按钮只在 `DOMContentLoaded` 里切换样式，没有任何代码读取选中的分类或发给后端（grep `dataset.cat`/`report_category` 无结果）。— [app/static/app.js:11244–11258](app/static/app.js)；[app/static/index.html:960–970](app/static/index.html)
- 同事周报勾选率：Kelley 85%、Amy 52%、Hamid 47%。— [trosa_current_state.md 第五节](../产品方向深度调研/trosa_current_state.md)

### Inferences
- 建议做法：查询改为"本周 `source='manual'`（及扩展/Inbox 人工确认来源）的全部记录，除去 `payload.weekly_excluded=true`"；Sela 来源默认不进。不需要迁移（payload 写入 + 应用层查询，但现有查询走视图，若要在视图里读 payload 仍需迁移，可与 C 合并）。
- 分类可直接由 C 的"事件标记"推出（报价→报价进展，寄样→样品寄送，PI/定金→订单签约），删掉那组不保存的按钮。
- 风险：改变同事唯一稳定的使用动机，需先问团队；历史周的显示会变化（过去未勾选的记录会出现），应只对新周生效。

### Gaps
- 周会是否固定周一、团队是否接受"排除制"需人判断。

## 五、功能 E/F/G：轻量报价行、轻量订单卡、同事邮箱捕获

### Takeaway
三者都没有现成数据结构。E（报价行）可以先挂在报价记录的 `payload` 上起步（S–M），F（订单卡 + 日期节点）需要新表（M–L，T2），G（IMAP）可大量复用已停用的 Gmail 同步模块的骨架（按成员加密存凭据、只按精确邮箱匹配、存原文出处、定时任务），只需换掉"取信"那一层（M–L，4–5 天），但涉及存储同事邮箱密码/应用专用密码，安全与同意要先定。

### Cited Findings
- 迁移里没有报价、订单、PI、付款、出货相关的表。— [trosa_current_state.md 第一节](../产品方向深度调研/trosa_current_state.md)；[migrations/](migrations/)
- 客户文件上传已有，带 `category` 字段（≤30 字），单文件 ≤25MB。— [app.py:11973–12045](app.py)
- `gmail_sync.py`（1,455 行）：按成员存连接记录（`_load_record/_save_record`），刷新令牌用 Fernet 加密（`_token_cipher/_encrypt_refresh_token`），只按规范化精确邮箱匹配联系人（`_matches_for_emails`），匹配不上进 Inbox，写时间线时保存原文出处（`_insert_source` → `trosa.communication_sources`），AI 摘要失败有兜底，定时任务按间隔调度。模块说明："不发信、不改邮箱、不建客户/联系人/待办"。当前未配置、已停用。— [gmail_sync.py:1–40、93–1455](gmail_sync.py)；[scheduler.py](scheduler.py)；[AGENTS.md](AGENTS.md)
- 全仓 Python 代码里没有 IMAP 实现。— grep 结果（本次）
- 读模型按成员隔离（`compat_current_user()`），成员默认只看到自己的时间线；周报对全员可见。— [migrations/0034_customer_history_binding.sql](migrations/0034_customer_history_binding.sql)；[app.py:22146–22190](app.py)

### Inferences
- E：报价记录的 `payload.quote_lines=[{材料, 厚度, 颜色, 尺寸, 单价, 单位, 币种, 贸易术语, 有效期}]`，客户首屏读"最近一条带报价行的记录"；有效期直接成为 A 的候选规则（"报价 X 天后到期"）。AI 从已发报价邮件/PDF 抽取需要 PDF 文字提取（`pdf` 解析库是否已在 requirements 未核对）。约 3 天。
- F：新表（订单卡：PI 号、金额、币种、付款条款、提单号）+ 节点（类型、计划日、实际日）；节点到期可作为 A 的规则。约 4–5 天，T2。金额可见范围要老板定。
- G：复用 Gmail 骨架，取信换成 `imaplib` + 应用专用密码；需要新增"按成员加密存密码"（可复用 Fernet 那套，但密钥管理在正式环境的环境变量里）。正文"仅本人可见"在现有按成员隔离下基本天然成立。风险：企业邮箱 IMAP 是否开放、密码轮换、首次回补量。

### Gaps
- 同事用什么邮箱（Gmail/网易/企业邮）未知，决定 G 是否可行。
- 未核对 requirements.txt 是否已有 PDF 解析依赖。

## 六、隐藏前提：系统到底知不知道"谁最后说话"，`contact_state`/`next_task_date` 能不能信

### Takeaway
系统**有**方向字段（`trosa.timeline_events.direction`，默认 `unknown`），外联邮件一律算 outbound，Sela 交接的回信是 inbound——这两类可信；但同事手工记录近 30 天有 40–54% 是 `unknown`、另有大量 `two_way`，用它判断"我们回了没有"不可靠。`contact_state` 只是"有没有过客户回话"的二值，`waiting_reply` 只看外联邮件，`next_task_date` 只是最早一条未完成待办——它们都不能代表真实跟进状态，候选计算必须直接读时间线。

### Cited Findings
- 时间线表 `trosa.timeline_events`：`event_type`（就是界面上的"沟通方式"）、`direction`（默认 `'unknown'`）、`content/result/next_plan`、`source_module`、`occurred_at`、`payload jsonb`。— [migrations/0001_unified_trade_os.sql:203–217](migrations/0001_unified_trade_os.sql)
- 读模型 `trosa.customer_interactions` 把沟通记录（`kind='communication'`，方向取原值）和外联邮件（`kind='email'`，方向写死 `'outbound'`）合在一起，**不暴露 `payload`**。— [migrations/0034_customer_history_binding.sql:75 起](migrations/0034_customer_history_binding.sql)
- 记录弹窗默认"自动识别"方向，可手动改为"我发给客户/客户发给我/双方沟通"；AI 整理在原文没有可靠方向时会推断并标 `direction_source='ai'`、`direction_confidence='medium'`。— [app/static/index.html:781](app/static/index.html)；[app.py:14953–14990](app.py)
- `contact_state` = `'contacted' if has_contact else 'uncontacted'`；`has_contact` 来自任一 inbound/two_way 记录或已回复邮件；`waiting_reply` 只在最新一条往来是外联邮件且状态为 `pending/no_reply` 时为真。— [trosa_domain.py:1960–1995](trosa_domain.py)
- 近 30 天手工记录的方向分布见第一节；90 天窗口里迁移数据的方向大多为 unknown（Amy 1573/1662、Kelley 1087/1149）。— [只读库查询 2026-10-27](tools/trosa_maint_read.py)；[trosa_current_state.md 第五节](../产品方向深度调研/trosa_current_state.md)
- 用户记忆里也记着"Trosa 沟通口径不可信：contact_state/next_task_date 别信，读时间线"。— 用户 auto-memory `trosa-contact-state-unreliable.md`（本次未打开原文，仅见索引）

### Inferences
- "客户来信未回"这条规则要么只对可靠方向（Sela 回信、Inbox 捕获、扩展采集、带发言人的 WhatsApp 导出）生效，要么等功能 B 把"按条捕获、每条带发言人"变成常态后才对同事有用。这意味着 **B 是 A 的数据前提之一**，不只是并列功能。
- 一条 `two_way` 记录里谁最后说话，可以在记录时让 AI 多抽一个字段"最后发言方"存进 `payload`，不改表结构；但要能在读模型里用，就得改视图（迁移，T2）或另写一条直读 `timeline_events` 的查询。

### Gaps
- 没有核对浏览器扩展与 Inbox 捕获写入时 `direction` 的实际取值分布（来源标签里没有单独的"扩展"标记，现状调研已指出）。

## 七、每个功能要动哪些测试/验收脚本，以及在多 Agent 流程下的发布成本

### Takeaway
发布按 diff 自动分级：只改 `app.py`/`app/static`/`scheduler.py` 等是 T1（门禁绿就自动发布）；**只要碰 `migrations/` 就是 T2，停在 awaiting-approval 等人工放行**；只改 tests/docs 是 T0（不部署）。每个任务都要走 独立 worktree → `[id]` commit → `sync` → `test`（完整门禁）→ 发布角色 `publish`，开发 Agent 不能自己发布；界面改动还要按真实浏览器重现规范留证据。所以最省成本的顺序是把所有需要新表/改视图的东西（A 的建议表、C 的事件标记视图列、F 的订单表）**合并成尽量少的 T2 迁移**，其余拆成小的 T1 任务。

### Cited Findings
- 分级规则：T2 = `migrations/`、`deploy/`、`serve.py`、`config.py`、`AGENTS.md`、名称含 release/migration/backup/restore 的工具；T0 = 只改 `docs/`、`design/`、`tests/` 或 `.md`；其余 T1。开发期本地门禁：只改 docs/design/tests/tools 走 fast，其余走 full。— [tools/release_tier.py:1–60](tools/release_tier.py)
- 流程：`ship`（sync + 快速门禁 + 登记队列）/`sync`（消解迁移编号碰撞 + 变基）/`test`（完整门禁并写证据）；"先 sync 后 test"；完成定义是 `status=landed`；T2 停在 `awaiting-approval` 人工放行。— [MULTI_AGENT_WORKFLOW.md §4–5](MULTI_AGENT_WORKFLOW.md)
- 迁移规则：编号懒预留（`reserve-migration`），forward-only，已应用文件不得改内容（运行时校验 SHA-256）。— [MULTI_AGENT_WORKFLOW.md §6](MULTI_AGENT_WORKFLOW.md)；[migrations/README.md](migrations/README.md)
- 新表还要登记进 PostgreSQL 关系契约 `REQUIRED_TABLES`，否则启动就绪检查对不上。— [postgres_schema_contract.py:14–35](postgres_schema_contract.py)
- 界面缺陷/验收：`tools/repro_*.cjs`（Playwright Chromium，退出码 0 通过 / 20 缺陷存在 / 21 环境故障），证据存 `docs/repro/<场景>/`；参考样板 `tools/repro_today_next_step.cjs`（今天页记录沟通 + 安排下一步）、`tools/repro_weekly_return.cjs`。— [docs/REAL_BROWSER_REPRO.md](docs/REAL_BROWSER_REPRO.md)；[tools/repro_today_next_step.cjs](tools/repro_today_next_step.cjs)
- 相关现有测试：`tests/test_today_timezone.py`（5 个）、`tests/test_today_dedupe_regression.py`（4）、`tests/test_weekly_page_visual.py`（5）、`tests/test_risk_regressions.py`（154，覆盖面最大）、`tests/test_runtime_contract.py`（8）、`tests/test_migration_integrity.py`（10）、`tests/test_outbound_budget.py`、`tests/test_agent_cli.py`、`tests/test_agent_data_freedom.py`、`tests/test_communication_text.py`、`tests/test_gmail_sync.py`、`tests/test_browser_extension.py`、`tests/test_task_graduation.py`；慢模型桩 `tools/repro_slow_provider.py`；浏览器验收 `tools/browser_acceptance.sh`、`tools/run_browser_acceptance.cjs`。— [tests/](tests/)；[tools/](tools/)
- AGENTS.md 要求 UI 改动在宽屏、iPad、iPhone、旧 Win10、键盘、200% 缩放与性能模式下真实浏览器验收；用户可感知改动写进 `CHANGELOG.md`。— [AGENTS.md](AGENTS.md)

### Inferences（按功能列测试与发布成本）
| 功能 | 需要新增/改动的测试与脚本 | 发布级别 | 发布成本估计 |
|---|---|---|---|
| A1 规则候选（纯后端、影子名单） | 新 `tests/test_followup_candidates.py`（SQLite 夹具：报价无回音、寄样无回音、Sela 客户排除、已有待办不重复、方向 unknown 不误报）；跑 `test_today_*`、`test_risk_regressions` | T1（若只做 CLI/工具则可放 `tools/` 走 fast） | 低 |
| A2 建议表 + 处理/稍后/安排 | 新迁移 + `postgres_schema_contract.py` + `test_migration_integrity`、`test_runtime_contract`；接口测试（处理/静音/安排变成待办且可撤销） | **T2** | 高（人工放行；建议和 C 的视图改动合并为一次） |
| A3 每日任务 + 模型排序 | 调度器单元测试；用 `tools/repro_slow_provider.py` 测慢/失败模型时兜底；`test_outbound_budget` | T1 | 中 |
| A4 今天页建议区 UI | 新 `tools/repro_today_suggestions.cjs`（修前 20、修后 0）+ `docs/repro/today-suggestions/`；多端截图 | T1 | 中（多端验收耗时） |
| A5 "问问这个客户" | 接口测试（只读、不写库、无 AI 时有明确提示）；复用 `test_agent_data_freedom` 的隔离断言；浏览器脚本 | T1 | 中 |
| B 10 秒录入 | `test_communication_text`、analyze-reply 提示词字段测试；手机宽度浏览器脚本；若加 Manifest/分享目标需新增静态文件与路由测试 | T1 | 中 |
| C 事件标记 | 写入/读取 payload 测试；标签映射测试；视图暴露列需迁移 | T1 + **T2**（与 A2 合并） | 中–高 |
| D 周报排除制 | `test_weekly_page_visual`、`test_weekly_lan_gateway`、`repro_weekly_return.cjs` 改写；Sela 来源不进周报的断言 | T1（若只改应用层） | 低–中 |
| E 报价行 | payload 校验测试；客户首屏浏览器脚本 | T1 | 中 |
| F 订单卡 | 新表迁移 + 契约 + 接口与浏览器测试 | **T2** | 高 |
| G IMAP 捕获 | 仿 `test_gmail_sync.py` 写 IMAP 假服务器测试；凭据加密测试；若要新环境变量（密钥）可能触及 `config.py`/部署配置 | T1，碰 `config.py`/`deploy/` 则 **T2** | 高 |

- 能在动手前用真实数据验证的（只读通道即可，零发布）：①A 的四条规则每人每天会出几条、有多少是同事认可的——把"影子名单"给老板和同事看一周；②方向字段可靠度（本笔记第一、六节已有初步数字）；③C 的历史回填会把多少旧记录标成报价/寄样；④D 排除制下每人每周会多出多少条记录（尤其 Hamid 的 Sela 来源）；⑤E/F 只需要访谈和看同事现有 Excel。
- 建议顺序：A1 影子名单（无迁移）→ B 的"默认整理 + 抽日期/事件字段"（给 A 喂数据）→ A2+C 合并一次 T2 迁移 → A3/A4 → A5 → D → E → F/G 视访谈结果再定。

### Gaps
- 没有实际运行完整门禁，单次 `test` 的真实耗时未测。
- 未确认发布流水线对 T2 的人工放行平均要等多久（取决于老板何时处理）。
