# 全球 CRM 与"让人每天自愿打开"的工作工具设计调研

> 调研日期：2026-10-27。范围：CRM 为什么被销售讨厌、哪些轻量/关系型 CRM 和工作工具被喜欢、自动记录的真实效果、习惯养成设计、"销售自己受益、管理者顺带看见"的团队机制。
> 来源说明：很多数据来自厂商博客（自带推销立场），已在条目中标注。早于 2023 年的来源会标【旧】。

## 一、CRM 失败与被冷落的证据

### Takeaway
"销售大部分时间不在卖货"这件事有可靠的一手数据（Salesforce、Gartner）：约 70% 时间耗在非销售事务上，一半销售被工具数量压垮，被压垮的人完成配额的概率低 45%。但流传很广的"CRM 项目 X% 失败"数字大多来自 2001–2007 年、出处模糊，不建议当作硬证据引用。

### Cited Findings
- Salesforce 第 6 版 State of Sales（2024，5,500 名销售、27 国）：行政事务、会议准备等非销售工作占销售时间的 70%，且与上一版"几乎没变"；67% 的销售预计今年完不成配额；81% 的销售团队在试用或已全面使用 AI。 — [Salesforce: Sales AI statistics 2024](https://salesforce.com/news/stories/sales-ai-statistics-2024)
- 二手来源称 Salesforce 第 7 版（2026，4,050 人、22 国，2025 年 8–9 月调研）里销售时间约 40% 在卖、60% 在非销售工作；时间主要被行政、数据录入和内部事务吃掉；Z 世代销售约 35% 时间在卖、每周约 2 小时花在手工录入。我没能打开 Salesforce 原始页面核实（Salesforce 官网报告页只显示"九成团队使用或两年内预期使用 agent"）。 — [CloudTalk 统计汇编](https://www.cloudtalk.io/blog/sales-productivity-statistics)；[Landbase](https://www.landbase.com/blog/sales-reps-30-percent-time-selling-2026)；[Salesforce State of Sales 页面](https://www.salesforce.com/sales/state-of-sales/)
  - 注意冲突：第 6 版是"30% 在卖"，二手引用的第 7 版是"40% 在卖"。也有博客把旧的 28% 数据（约 2018 年【旧】）误当作最新版引用。
- Gartner（2024-09 新闻稿，1,026 名 B2B 销售，2024 年 1–3 月调研）：50% 销售觉得工作需要的技术数量让他们不堪重负；72% 觉得工作需要的技能太多；"被压垮"的销售完成配额的可能性低 45%。会有效与 AI 协作的销售完成配额的可能性高 3.7 倍（这是相关性，不是因果）。 — [Gartner 新闻稿](https://www.gartner.com/en/newsroom/press-releases/2024-09-16-gartner-sales-survey-reveals-sellers-who-partner-with-ai-re-three-point-seven-times-more-likely-to-meet-quota)
- Gartner 更早一份销售生产力调查：49% 被技术压垮，压垮者完成配额概率低 43%。 — [Gartner: Sellers need tech as a teammate](https://www.gartner.com/en/sales/trends/sellers-need-tech-as-a-teammate)
- "CRM 失败率"数字：Gartner 2001 年预测 2006 年前 50% CRM 实施会被判定失败；另有文章称最高 60%，主要原因是"管理层/用户不买账"和后端系统集成复杂。【旧，2001–2007】 — [eWeek: Playing the CRM Odds](https://www.eweek.com/fr/enterprise-apps/playing-the-crm-odds/)
- 2004 年 Gartner 分析师 Ed Thompson 自己说这些失败率很难追溯到真实报告，问题在于客户把 CRM 当一堆技术而不是业务策略。【旧，2004】 — [CustomerThink 访谈](https://customerthink.com/reports_crm_failure_highly_exaggerated/)
- 各种调查平均"失败率"约 46.3%，但口径差异极大。 — 同上 eWeek 系列/[Workbooks](https://workbooks.com/?p=259)（汇编，可信度低）
- "只有约三分之一 CRM 项目在销售团队达到 90% 以上使用率"归于 MHI Global，原始研究找不到。 — [Nutshell 博客](https://www.nutshell.com/blog/reasons-why-sales-reps-quit-their-crms)（厂商）
- "Gartner 称最多 42% 的 CRM 许可证闲置"——无法在 Gartner 原文核实。 — 见上方 Gartner 搜索结果中的二手转引（不建议使用）
- HubSpot 数据：被二手站引用"32% 的销售每天花 1 小时以上往 CRM 录数据"，年份与原报告无法核实；HubSpot 2014–15 年报告中 20%+ 销售说手工录入是 CRM 最大痛点【旧】。 — [SalesLion 引述](https://saleslion.io/sales-statistics/32-of-sales-reps-spend-an-hour-or-more-on-data-entry-every-day/)；[HubSpot 博客](https://blog.hubspot.com/sales/manual-data-entry-is-salespeople%E2%80%99s-chief-crm-complaint)
- 销售避开 CRM 的典型原因（厂商/行业博客的共同说法）：
  - 录入成本是"立刻的、自己的"，好处是"延后的、别人的"（管理者的预测与报表）。 — [Attention 博客](https://wf-origin.attention.com/blog-posts/sales-teams-hate-crm-data-entry-heres-how-to-make-it-effortless)（厂商）
  - 平均 Salesforce 用户每周约 4 小时录数据（二手引用）。 — [GTMnow](https://gtmnow.com/?p=81262)
  - "列太多、点击太多"；成交日期过期、下一步空白、价格不更新。 — [Vymo](https://vymo.com/blog/?p=19257)；[Attention](https://wf-origin.attention.com/blog-posts/sales-teams-hate-crm-data-entry-heres-how-to-make-it-effortless)
  - CRM 被设计成"管理者工具"——适合集中存客户信息、给高层做预测，但不帮销售卖货。 — [Mediafly](https://www.mediafly.com/?p=24404)；[Accent Technologies 信息图【旧，2016】](https://accent-technologies.com/2016/05/05/why-sales-reps-hate-crm-infographic/)
  - 与现有工具集成差，增加步骤。 — [Nutshell](https://www.nutshell.com/blog/reasons-why-sales-reps-quit-their-crms)

### Inferences
- 真正可靠的证据指向同一件事：**问题不是"销售懒"，而是"录入的人不受益"**。数据录入 + 工具数量 = 负担；负担与业绩负相关（Gartner）。
- 对 Trosa：四人团队里"录入者 = 使用者"，天然比大公司 CRM 好；但如果周报页让人感觉"填表是为了被看"，同样的厌恶会出现。
- 不要在 Trosa 文档里引用"CRM 70% 失败"之类数字，出处站不住。

### Gaps
- 没找到 Salesforce 第 7 版原始页面，"40% 在卖"只能算二手。
- 没找到 CSO Insights（已被 Korn Ferry 收编）2023 年后的 CRM 采用数据。
- 没成功抓到 Reddit r/sales、r/CRM 原帖，用户情绪只能用厂商博客和 G2/Capterra 汇总替代。

## 二、被喜欢的轻量/关系型 CRM 与工作工具：各自的"每日吸引力"来源

### Takeaway
被喜欢的工具几乎都做到三件事之一：①住在你本来就待的地方（Gmail、WhatsApp），②让"今天做什么"不用想（Pipedrive 的活动、Superhuman 的收件箱清零），③快且有主见（Linear、Superhuman），少给选项。

### Cited Findings
**Pipedrive（活动驱动销售）**
- 2010 年由做了多年销售培训的 Timo Rein、Urmas Purde 创立；核心理念"结果控制不了，但活动可以控制"，产品围绕"安排—完成—追踪活动"。 — [Pipedrive: Activity-Based Selling 指南](https://www.pipedrive.com/en/blog/activity-based-selling)（创始人撰写，带推销）
- 当时主流 CRM 围绕"结果"设计，Pipedrive 则标出"没有安排下一步活动的单子"；看板式管道是组织原则而不只是一个功能。 — [Stacksync 起源故事](https://www.stacksync.com/blog/the-brain-surgery-and-the-accelerator-the-origin-story-of-pipedrive)（叙事化，细节需谨慎）

**Close（为销售而不是为管理者造）**
- Steli Efti 原本经营外包销售团队 Elastic Sales，工程合伙人先为自家销售做内部工具，2013 年发布为"销售人员做给销售人员"的 CRM，核心是打电话和发邮件。 — [Stacksync: Close 起源](https://www.stacksync.com/blog/the-seven-rejections-the-origin-story-of-close)（叙事化）
- 播客主题"为使用者设计，而不是为买单的人设计"。 — [ServiceRocket 播客 Ep.61](https://blog.servicerocket.com/podcasts/ep-61-steli-efti-on-designing-software-for-the-user-not-the-buyer)
- 产品内置通话、短信、邮件序列和预测式拨号；公司自述专注"沟通本身，不要手工录入、不要复杂界面"；自 2016 起盈利、未融资（收入数字各来源冲突，不引用）。 — [Welcome to the Jungle 公司页](https://app.welcometothejungle.com/companies/Close)；[Indie Hackers 访谈](https://www.indiehackers.com/podcast/053-steli-efti-of-close-io)

**Streak（CRM 住在 Gmail 里）**
- G2 用户评价汇总：最被称赞的是和 Gmail 的无缝集成，不用切换平台；小企业用户说"不用换工具就能管单子和跟进"。 — [G2 Streak 评价](https://g2.com/products/streak/reviews)（G2 摘要为 AI 生成）
- Capterra 4.5/5（479 条），学习曲线低、"功能想用多少用多少"；缺点：涨价、复杂管道时报表弱、偶尔加载慢。 — [Capterra Streak](https://www.capterra.com.au/reviews/124465/streak)；[Hack'celeration 评测 3.7/5，"轻量好用但很快碰到天花板"](https://hackceleration.com/labs/review/streak)

**folk（关系型、跨渠道）**
- 从 LinkedIn、邮箱、WhatsApp、日历导入联系人；"跟进助手"读邮件和 WhatsApp 对话，找出停滞的对话并建议个性化跟进消息。 — [Dupple 评测 2026](https://dupple.com/reviews/folk)；[Waalaxy 页面](https://www.waalaxy.com/exclusive-saas-deals/folk-promo-code-exclusive-deal)（推广页）
- 有手机号时一键打开 WhatsApp；folkX 插件一键从 LinkedIn 存联系人并后台补全资料。 — [folk: WhatsApp CRM 集成](https://www.folk.app/articles/best-crm-integrations-with-whatsapp-features-and-benefits-explained)；[folk LinkedIn CRM](https://folk.app/articles/best-linkedin-crm)
- G2 4.5/5（318 条），夸易用；不适合大批量交易型销售或复杂预测。 — [G2 folk](https://www.g2.com/products/folk-folk/reviews)；[Dupple](https://dupple.com/reviews/folk)

**Attio（同步为底座 + 灵活数据模型）**
- 默认为你邮件往来和日历上的人自动建档，并按邮箱域名关联公司；默认"选择性建档"——对方来信你没回过就不建档。 — [Attio 帮助：邮件与日历同步](https://attio.com/help/reference/email-calendar/email-and-calendar-syncing)
- 自动填充"联系强度""最近一次互动""下一次日程"，并显示团队里谁和这个客户关系最强。 — 同上
- 2019 年 Nicolas Sharp 创立，出发点是传统 CRM 数据结构太死板；2023 年融资 2350 万美元。 — [TechCrunch 2023](https://techcrunch.com/2023/03/02/attio-raises-23-5m-to-build-a-next-gen-crm-platform/)；[Craftt 评论](https://www.craftt.io/blog/why-attio-crm)

**Affinity（关系自动捕获，偏投资机构）**
- 自称自动记录所有邮件、日历、会议，"无需手工录入"，汇成团队的关系图谱；有按邮箱跨 40+ 数据源的去重匹配，合并前可预览；敏感交易可设单独可见名单。 — [Affinity 产品页](https://www.affinity.co/product?utm_id=b2saas)；[Affinity 隐私控制发布](https://www.affinity.co/product-releases/enhanced-privacy-controls-for-activity-data)（全部为厂商说法）

**Day.ai / Lightfield（AI 原生、"零录入"）**
- Day.ai 创始人 Christopher O'Donnell 是前 HubSpot 首席产品官，目标是消除手工录入，同时让用户对 AI 记录"透明可控"。 — [Sequoia Inference 播客](https://inferencebysequoia.substack.com/p/why-crm-needs-an-ai-revolution-with-0bf)
- Lightfield 由 Tome 创始人（Keith Peiris、Henri Liriani，前 Meta）转型而来，读邮件、会议、通话自动建 CRM；他们说真正的问题是"销售缺少客户记忆"。 — [SaaStr](https://www.saastr.com/saastr-ai-app-of-the-week-lightfield-the-ai-native-crm-that-killed-tomes-25-million-users-to-build-something-better/)；[Contrary Research](https://research.contrary.com/company/lightfield)
- 两家"零录入"都只有厂商/投资方叙述，未见独立验证。

**Superhuman（速度 + 收件箱清零）**
- PMF 引擎：问"如果不能再用这个产品你会有多失望"，"非常失望"≥40% 视为达到 PMF（Sean Ellis 基于约 100 家公司的经验值）。Superhuman 初始 22%，聚焦目标人群后 33%，约三个季度后 58%。【2018 年文章，旧但仍是经典】 — [First Round Review](https://review.firstround.com/how-superhuman-built-an-engine-to-find-product-market-fit/)
- 最忠实用户说的头号价值是"速度"；团队把界面响应从 100ms 压到 50ms 以内，搜索比 Gmail 快；增加快捷键并"缓冲按键"防止打字快时丢输入；用户原话"几乎不用触控板"。 — 同上
- 路线图一半强化粉丝喜爱的（速度、快捷键、自动化、设计），一半解决犹豫者的障碍（移动端、集成、附件、日历、搜索）。 — 同上
- 目标用户画像追求 Inbox Zero，但一周最多做到两三次——说明"清零"是愿景，产品要让人经常接近它。 — 同上

**Linear（有主见的简单）**
- 联合创始人 Jori Lallo："我们把它设计成只有一种真正好的做法"；"没人想浪费时间纠结流程细节"。 — [Figma 博客: The Linear Method](https://www.figma.com/blog/the-linear-method-opinionated-software/)
- 产品负责人 Nan Yu 的分享（二手整理）：即使管理者要求，也抵制"必填字段""多负责人"之类选项，因为会伤害一线使用者的工作流；速度来自熟练而非偷工。 — [Antoine Buteau 整理](https://www.antoinebuteau.com/lessons-from-nan-yu-head-of-product-at-linear/)（二手）
- 强默认值降低决策疲劳。 — [DevTools Brew 访谈整理](https://morganperry.substack.com/p/devtools-brew-62-building-linear)（二手）

**个人 CRM（Dex / Clay.earth）**
- Dex 以提醒为核心，在关系"变冷"前提示联系；"自动化提醒，不自动化关系"。Clay 偏数据，资料最丰富，但有用户花在整理联系人上的时间多过真正联系。 — [Dex vs Clay（Dex 自家博客）](https://getdex.com/blog/dex-vs-clay)；[Goodword 对比](https://www.goodword.com/blog/clay-vs-dex-vs-goodword)
- 注意：此处 Clay 指个人 CRM Clay（clay.earth），不是数据补全工具 Clay.com。

### Inferences
- 每个工具的"每日吸引力"一句话版：
  - Pipedrive：每个客户都必须有"下一步"，没有就标红——今天要做的事自动浮出。
  - Close：打电话/发消息就在 CRM 里，记录是沟通的副产品。
  - Streak：不离开邮箱。
  - folk：替你发现"聊着聊着断了"的对话，并起草跟进。
  - Attio/Affinity：不用录，系统知道你最近和谁联系过、谁最熟。
  - Superhuman：快到让人想清空；每个动作一个键。
  - Linear：一种好做法，不给管理者加必填项。
  - Dex：关系变冷前提醒。
- 对 Trosa 最贴近的组合：**Pipedrive 的"下一步必须有日期"（Trosa 已有：待办=动作+日期）+ folk 的"停滞对话提醒"+ Superhuman 的速度/键盘/清零感 + Linear 对必填项的克制**。
- Trosa 客户沟通主要在 WhatsApp/微信，folk 的 WhatsApp 方案是"一键打开 + 读对话找停滞"，而不是完整同步——这对 Trosa 的"手工记录 + Inbox 判断"更现实。

### Gaps
- Copper（Google Workspace 内 CRM）、Front/Missive（共享收件箱）、Things/Todoist 本轮没有检索到可靠的 2023+ 来源，未写入；它们的公认卖点（Copper 嵌在 Gmail 侧栏、Front 团队共享收件箱+内部评论、Things 的"今天/计划/某天"分区）属于常识性描述，需另行核实。
- Superhuman 的具体功能（Split Inbox、Remind me、提醒回复）本轮未抓到官方页面。
- Day.ai、Lightfield 没有独立用户评价。

## 三、减少录入负担：自动捕获到底好不好用

### Takeaway
自动捕获能显著减轻录入，但"零录入"是营销说法：匹配靠邮箱精确对应，换邮箱、抄送、无匹配都会出错；需要建档规则、屏蔽名单、默认隐私和"待人工归属"区。最成熟的做法（Attio）是默认保守：只为你主动回过的人建档、正文默认私有。

### Cited Findings
- Salesforce Einstein Activity Capture 按 To/From/CC/参会人邮箱匹配联系人；匹配不上的进入"内部活动流"等人工关联。 — [Revenue Grid](https://revenuegrid.com/blog/einstein-activity-capture/)（竞品厂商）
- 竞品博客称匹配"经常出错"：抄送邮件重复计数、对方换邮箱后整条线程脱钩；日历重复事件是已知问题，通常建议单向同步。 — [Revenue Grid: EAC limitations](https://revenuegrid.com/blog/einstein-activity-capture-limitations/)；[Weflow 帮助](https://help.getweflow.com/en/articles/12067368-important-information-if-you-are-using-einstein-activity-capture)
- EAC 捕获的邮件默认存在 AWS 而不是标准对象里，报表/流程读不到；Standard 版保留 6 个月，到期删除。 — [Revenue Grid](https://revenuegrid.com/blog/einstein-activity-capture-limitations/)；[MassMailer](https://massmailer.io/blog/einstein-activity-capture-alternative/)（细节可能已变）
- Attio 的防噪声/隐私机制：
  - 建档三档：全部 / 选择性（默认，对方来信你没回过不建档）/ 不自动建档。
  - 公共邮箱域名（gmail.com 等）不自动建公司。
  - "记录抑制"阻止特定地址/域名建档；"屏蔽名单"隐藏某地址/域名的往来。
  - 默认只向团队共享时间、参与人、主题，正文私有；内部邮件和私人日历事件不同步。 — [Attio 帮助](https://attio.com/help/reference/email-calendar/email-and-calendar-syncing)
- Affinity 用跨 40+ 来源的邮箱匹配去重并在合并前预览；它自己的 FAQ 把"AI 输出能否在改记录前审阅"列为买家应问的问题。 — [Affinity AI](https://affinity.co/product/artificial-intelligence)
- 个人 CRM 经验：数据越丰富，越可能让人忙于整理而不是联系（Clay）。 — [Goodword](https://www.goodword.com/blog/clay-vs-dex-vs-goodword)

### Inferences
- 自动捕获的三大坑：**噪声**（通知邮件、群发、内部邮件）、**错配**（换邮箱、共用邮箱、抄送、同公司多人）、**隐私**（正文被全员看到）。
- 成熟方案的共同点与 Trosa 现有原则一致：只有唯一精确匹配才自动确认，其余进"待审阅"——Trosa 的 Inbox 就是这个"待人工归属区"，方向正确。
- Trosa 主战场是 WhatsApp/微信，没有官方可靠的自动同步通道；更现实的"减负"是：粘贴/转发一段对话 → AI 预填摘要和候选客户 → 人一键确认（Trosa 已有捕获→整理→确认链路）。
- 应借鉴 Attio 的"保守默认"：只为真正有往来的人建档；公共邮箱域名不建公司。

### Gaps
- 没有找到独立（非厂商）的自动捕获准确率数据，"零录入"效果无法量化。
- 没找到 WhatsApp Business 自动同步进 CRM 的错配/隐私问题研究。

## 四、工作工具的习惯与留存设计（不让人觉得被监视）

### Takeaway
能让人每天主动打开的机制，本质是"打开就省事"：一个起点页告诉你今天该做什么、能清空的队列给完成感、极快+键盘操作、关系变冷/对话停滞的提醒。证据多是实践案例（Superhuman、Pipedrive、Dex），缺少学术级研究。

### Cited Findings
- 速度本身就是留存理由：Superhuman 最忠实用户首要价值是速度，团队持续压缩到 50ms 以内并扩充快捷键。 — [First Round Review](https://review.firstround.com/how-superhuman-built-an-engine-to-find-product-market-fit/)
- "清零"是持续目标：目标用户一周只能清零两三次，但仍以此为追求。 — 同上
- 用"非常失望比例"衡量是否被需要，并作为产品团队唯一关键结果，持续每周/月/季度追踪。 — 同上
- 活动驱动：每个客户都要有已安排的下一步，没有就提醒——让"今天做什么"成为默认视图。 — [Pipedrive](https://www.pipedrive.com/en/blog/activity-based-selling)
- 停滞检测：folk 助手扫描邮件和 WhatsApp，找出冷掉的对话并建议跟进。 — [Dupple](https://dupple.com/reviews/folk)
- 关系变冷提醒：Dex 在太久没联系前提醒，但只提醒、不替你维护关系。 — [Dex](https://getdex.com/blog/dex-vs-clay)
- 强默认值、单一好做法降低决策疲劳，让人直接干活。 — [Figma: Linear Method](https://www.figma.com/blog/the-linear-method-opinionated-software/)
- 工具越多越伤业绩（Gartner 50% 被压垮、配额概率低 45%）——习惯设计不能靠增加通知和入口。 — [Gartner](https://www.gartner.com/en/newsroom/press-releases/2024-09-16-gartner-sales-survey-reveals-sellers-who-partner-with-ai-re-three-point-seven-times-more-likely-to-meet-quota)

### Inferences
- 对 Trosa 可直接落地的习惯机制（按与现有页面的贴合度排序）：
  1. **Today 作为唯一起点**：打开就是今天到期的承诺，做完一个少一个，有"今天清空"的完成感（类比 Inbox Zero）。
  2. **Inbox 可清零**：每条信号只有少数几种处理（确认/归到客户/忽略/稍后），清空后有明确的空状态。
  3. **"上次之后发生了什么"**：客户工作区顶部只显示自你上次查看后的新事实，而不是全部历史。
  4. **停滞提醒而非打分**：类似 folk/Dex——"某客户 X 天没动静且没有下一步"进入 Inbox，而不是给客户评级/健康分（Trosa 已明确不加状态/指标）。
  5. **键盘与速度**：常用动作单键可达、保存后局部更新（Trosa 已有局部更新原则）。
- 不像监视的关键：提醒只发给关系主人本人；内容是"你可能想做什么"，不是"你没做什么"；不做活动量排行榜。

### Gaps
- 未找到 Lenny's Newsletter / First Round 关于"B2B 每日习惯"的可公开全文（Lenny 留存文为付费墙）。
- 未找到关于"工作工具通知疲劳/被监视感"的定量研究。
- "what changed since I last looked"作为模式缺少专门来源，属于推断。

## 五、团队动态：先让销售受益，管理者视野作为副产品

### Takeaway
被喜欢的工具（Close、Linear、Streak）都明确"为使用者而非买单者/管理者设计"，并拒绝为管理者加必填项；管理者的可见性来自使用者自然留下的痕迹（沟通、下一步、关系强度），而不是额外填报。

### Cited Findings
- Close 起源叙述把 Salesforce 描述为"为想看仪表盘的企业管理者造，而不是为要打电话的销售造"；Close 则为自家销售先做内部工具。 — [Stacksync: Close 起源](https://www.stacksync.com/blog/the-seven-rejections-the-origin-story-of-close)（叙事化）；[ServiceRocket 播客："为用户而不是买家设计"](https://blog.servicerocket.com/podcasts/ep-61-steli-efti-on-designing-software-for-the-user-not-the-buyer)
- Linear 抵制管理者要求的必填字段/多负责人，以保护一线使用者。 — [Antoine Buteau 整理 Nan Yu](https://www.antoinebuteau.com/lessons-from-nan-yu-head-of-product-at-linear/)（二手）
- 销售不录入的根本原因是成本归自己、收益归别人（管理者预测）。 — [Attention](https://wf-origin.attention.com/blog-posts/sales-teams-hate-crm-data-entry-heres-how-to-make-it-effortless)（厂商）
- Attio 自动显示团队里谁与某客户关系最强、最近互动时间——管理者/同事的"可见性"来自同步数据，无需额外填报；同时正文默认私有。 — [Attio 帮助](https://attio.com/help/reference/email-calendar/email-and-calendar-syncing)
- 2001–2007 年的 CRM 失败分析已把"缺少管理层或用户买账"列为头号原因。【旧】 — [eWeek](https://www.eweek.com/fr/enterprise-apps/playing-the-crm-odds/)

### Inferences
- "销售优先"的检验标准：每一个录入动作，录入者本人在 24 小时内能否从中得到好处（提醒、上下文、少打字）？不能的字段就不该要求。
- 对 Trosa 周报页：应完全由日常记录自动汇总（谁推进了哪些客户、哪些承诺完成/延期），成员不需要为周报单独填写；措辞对事不对人，避免排行榜。
- 四人团队的协作价值主要是"接手不断档"（某人请假/离职时客户上下文完整），这是比"管理监督"更能被成员接受的理由。

### Gaps
- 未找到"销售优先 vs 管理者优先 CRM"对采用率影响的定量对比研究；以上都是案例与创始人观点。
