# AI 跟进建议引擎与"问助手"对话层：怎么做才会被信任、被使用

调研日期：2026-10-27。资料以 2023–2026 为主，较旧的已标注"旧数据"。很多资料是厂商自己的帮助中心或博客（有推销动机），已逐条注明；部分细节来自搜索摘要、未能打开原页核实的，也已注明。背景：Trosa 老板的判断是"业务员记录时真的不知道什么时候该跟"，希望 AI 每天自己扫一遍、提出"跟谁、为什么"，并能随口问"某客户是不是也可以跟进了？"。过去的 15/30/60 天固定节点失败，留下约 1,190 条没人认领的旧行（Hamid 约 583、Amy 306、Kelley 299）— [现状调研笔记](<../Trosa 产品方向深度调研/trosa_current_state.md>)。

## 一、现有工具如何决定"什么时候该跟进"、怎么呈现；规则 vs 模型；用户评价

### Takeaway
市面上几乎所有"跟进提醒"都只用一个核心信号：**最后一条消息是谁发的、沉默了多久**（再加上用户手设的间隔或阶段时限）。真正用 LLM 的地方主要是两件事：判断"我最后发的这条是否需要对方回"（Superhuman、Gmail），以及写跟进草稿（folk、Superhuman、HubSpot）。没有一家公开了可独立验证的"建议准确率/采纳率"数据；用户口碑好的产品都是**少而准、可关、可推迟**，被吐槽的都是"红点满屏/误报逾期"。

### Cited Findings

**邮件类（"我发了没人回"）**
- Gmail Nudges（2018 年上线）分两种：别人来信你没回（reply nudge）、你发出去对方没回（follow-up nudge），提示语形如"Sent 5 days ago. Follow up?"；默认开启，可在设置里分别关掉 — [9to5Google 2018（旧）](https://9to5google.com/2018/05/14/new-gmail-web-nudge-ai-reminders/)；[Ubergizmo 2018（旧）](https://ubergizmo.com/2018/05/gmail-update-nudge-users-respond-email)
- Gmail 用机器学习挑候选：看你的邮件习惯、和对方的往来历史、是否直接发给你；通常在沉默"2 到 5 天"后出现，但"不保证"；群发/邮件列表、已加星/已推迟/已归档、被过滤到其他标签的线程通常不会被提醒；用户不能指定哪些重要、不能设延迟；手机端"没有关闭按钮"。该文作者卖竞品工具，评价它对客户沟通"不是可靠的系统" — [Mailmeteor 2026-04（厂商博客）](https://mailmeteor.com/blog/gmail-nudges)
- Superhuman Auto Reminders：用户设默认提醒时长和范围（"需要跟进的邮件"/"所有外部邮件"/"不提醒"），可选只在工作日；"需要跟进"由 AI 根据**你最后一封外发邮件的内容**判断，对方没回才提醒；Business/Enterprise 版的 Auto Drafts 会在提醒时自动生成一封"你的口吻"的跟进草稿，由你审阅后发 — [Superhuman 帮助中心：Auto Reminders & Auto Drafts](https://help.superhuman.com/hc/en-us/articles/40144492186515)（原页 403，内容来自搜索摘要，约 2025-08）；[Reminders on Autopilot](https://help.superhuman.com/hc/en-us/articles/45270478397203-Reminders-on-Autopilot)；[Tom's Guide 评测](https://www.tomsguide.com/ai/superhuman-email-can-generate-emails-send-reminders-and-more-heres-how-to-sign-up)
- 未找到对 Superhuman"是否需要跟进"判断准确率的独立测试（同上来源说明）。

**关系类 CRM（"很久没联系了"）**
- folk AI Follow-ups（2025-01 上线，自称"迈向主动式 CRM 的第一步"）：扫描收件箱和日历，结合 CRM 数据挑"最值得跟进的线索"，按对话历史和你的写作风格生成草稿；页面没说明如何排序 — [folk changelog 2025-01（厂商）](https://www.folk.app/changelog/ai-follow-ups)；[Product Hunt 发布页](https://hunted.space/product/ai-follow-ups-by-folk)
- 现版 folk Follow-up Assistant 据描述：找"聊着聊着安静了、但下一步还悬着"的对话，覆盖邮件和 WhatsApp；可设置沉默多久才算、选哪些联系人/分组参与；建议出现在应用内、邮件和联系人页；邮件需 Google/Microsoft 账号且对话至少有两轮近期往来 — 来自搜索摘要引用的 folk 页面，[folk 帮助中心原文](https://help.folk.app/en/articles/10304768-ai-follow-ups) 无法打开核实。
- Dex（个人关系 CRM）Keep-in-touch：**纯规则**。每个联系人手选 1/3/6/12 个月间隔；以"最后一次互动"为起点计时，记录新互动就自动顺延；忽略一次提醒，下个周期再来（不会堆积）；早上发邮件/推送；**自动把提醒摊开，每天最多 5 条**；逾期在名字旁显示感叹号 — [Dex 指南：Keep-in-touch（厂商）](https://guide.getdex.com/features/keep-in-touch)；[Dex docs](https://getdex.com/docs/workflows/keep-in-touch)
- Dex 自己的口号是"自动化提醒，不自动化关系" — [Dex vs Clay（厂商）](https://getdex.com/blog/dex-vs-clay)（转引自上一轮报告）

**销售 CRM（"单子放烂了"）**
- Pipedrive Rotting：**纯规则**。每个管道阶段单独设"腐烂天数"，超过天数没更新的单子在看板上标红；计时以单子的"最后更新时间"为准（完成活动、收发/归档关联邮件都算更新）。官方提醒：时限太长影响短期目标，太短又难排程，建议不同周期的业务分不同管道 — [Pipedrive 帮助：The Rotting feature](https://support.pipedrive.com/hc/en-us/articles/206760949)
- 2022 年社区帖抱怨 Rotting 唯一的内建动作是"出一份腐烂单子报表"，想要自动化触发；未见后续改变的证据 — [Pipedrive 社区](https://community.pipedrive.com/discussion/1098/rotting-as-an-automation-trigger)（旧）
- Pipedrive AI Sales Assistant：一个"AI 通知流"，给提示、下一步、风险单子；可"今天暂停通知"、在设置里按类型管理，并可订阅每日汇总邮件（自选时间，汇总过去 24 小时）— [Pipedrive 帮助：Sales Assistant notifications（beta，荷兰语版）](https://support.pipedrive.com/nl/article/sales-assistant-notifications-beta)；其"赢单预测"会给赢单概率并推荐下一步 — [Pipedrive 功能页（厂商）](https://pipedrive.com/features/ai-sales-assistant)
- G2 上一位 Pipedrive 用户说它"每天提醒我该跟进谁"（依据是活动里人设的日期），同时报告了 bug：有些潜客被重复显示为"逾期"，而跟进其实排到了 2026 年 — [G2 评论](https://www.g2.com/products/pipedrive/reviews/pipedrive-review-12107342)。未找到专门评价 Sales Assistant 噪音程度的 G2 评论。
- Close：Inbox 分三栏——"现在要处理 / 已完成 / 未来（已排期或已推迟）"，进入 Inbox 的有：客户来信（含跟进提醒）、未接来电、来信短信、到期任务、到预计成交日的商机；Smart Views 是"保存的筛选"，客户随数据变化自动进出，官方建议每种日常动作建一个视图当待办清单用 — [Close 帮助：Inbox](https://help.close.com/docs/inbox)；[Close 帮助：Smart Views](https://help.close.com/docs/smart-views)
- Salesforce Einstein Next Best Action：推荐由"策略"（Strategy Builder，后改为 Flow）按规则筛选/排序产生，可接预测模型；每条推荐带"接受/拒绝"两个按钮，反应记录为 Accepted/Rejected，可按策略汇总接受/拒绝次数做报表；但标准报表不够用，社区里有客户另建对象来记录 — [unofficialsf：Reporting and Tracking with NBA](https://unofficialsf.com/reporting-and-tracking-with-next-best-action/)；[Salesforce 开发文档：Reactions（API 44，旧）](https://developer.salesforce.com/docs/atlas.en-us.216.0.chatterapi.meta/chatterapi/connect_resources_nba_reactions.htm)；[Trailblazer 社区](https://trailhead.salesforce.com/ja/trailblazer-community/feed/0D54S00000B2FnUSAV)
- HubSpot Breeze Prospecting Agent（Beta）：盯"购买信号"，信号触发后研究账户、找联系人、**起草外联邮件交给销售审阅，审过才发**；按每条推荐线索计费（来源说法不一）；"回复率与人工 SDR 相当""会议 +28%"均为厂商自述 — [HubSpot 产品页（厂商）](https://www.hubspot.com/products/sales/ai-prospecting-agent)；[resolve247 评测](https://resolve247.ai/blog/hubspot-breeze/)。跟进类建议主要靠 Breeze Assistant 对话生成，未找到独立评测。
- 上一轮调研汇总：Breeze 补全的公司信息常过时或错误、草稿"像努力装成人的公司机器人"；代理商建议 Beta agent"当作有前途的东西，不要当生产工具" — [LowCode Agency](https://www.lowcode.agency/blog/hubspot-breeze-ai-reliability-problems)；[InsideA](https://insidea.com/blog/hubspot/which-hubspot-breeze-agent)
- OKKI（小满）：公开资料能确认的是移动端"AI 推荐客户"、日程新建与提醒、营销旅程里"按客户回应状态自动推进跟进节奏"；**没找到**官方说明的"销售端 AI 跟进建议"功能 — [App Store](https://apps.apple.com/app/id1092357844)；[Alibaba 卖家博客（台湾站）](https://seller.alibaba.com/tw/blogs/okki-crm-ai-customer-management-system)。上一轮调研另见 OKKI 能从聊天/邮件里认出 PI、报价、付款进度生成草稿 — [OKKI 更新日志（厂商）](https://www.okki.com/upgrade.html)

### Inferences
- 规则 vs 模型的分工很清楚：**"该不该提醒"几乎都是规则**（沉默天数、阶段时限、人设间隔），**模型只做两件窄事**——判断"我最后那句话是不是在等对方回"（是提问/报价/寄样通知，还是"谢谢、收到"），以及写草稿。Trosa 可以照抄这个分工：用确定的事实算候选，用 LLM 判断"这条对话是否悬着"和写"为什么现在 + 可以说什么"。
- Dex 的三个细节正好针对 Trosa 15/30/60 的死因：**以最后一次真实互动为起点**（有新沟通就自动顺延）、**忽略就等下个周期而不是累积成逾期**、**每天封顶 5 条**。
- 15/30/60 的问题不在"天数"，而在"按日历生成、不看有没有新沟通、不会自己消失"。Pipedrive 的 Rotting 也是天数，但它只在看板上变色，不生成任务，所以不产生债务。
- 用户夸的都是"替我记住了"，骂的都是"误报"（Pipedrive 重复逾期 bug、Gmail 不可控）。误报一次伤害远大于漏报一次。

### Gaps
- 没有任何厂商公开建议的采纳率/准确率；Salesforce 有接受/拒绝的记录机制，但没找到公开的汇总数字。
- folk Follow-up Assistant 的帮助中心无法打开，"两轮往来""可设沉默天数"等细节未能核实。
- Attio "last interaction"字段如何驱动提醒、Clay/UpHabit 的提醒机制、Streak 的跟进提醒，本次未查到一手说明。
- Reddit/G2 上针对"AI 跟进建议有用还是噪音"的一手讨论本次没有抓到。

## 二、B2B 跟进节奏的证据：几次、隔多久、回复速度；外贸专属经验

### Takeaway
可靠的量化数据**几乎全部来自冷开发邮件**（陌生人外联），不是"已报价/已寄样的熟客户"；其结论（3–5 次触达、第一封回复率最高但后续封贡献过半回复）对 Sela 有用，对 Trosa 熟客跟进只能参考。"越快回复越好"的著名数据是 2007/2011 年的旧研究，针对的是网站询盘。外贸节奏（报价后 1–2 天、寄样后按物流 + 测试期、斋月前推进、节后再跟）**全部是从业者经验，不是数据**。

### Cited Findings

**冷开发邮件（数据，但场景不同）**
- Belkins 2026 版研究（2025 全年 753 万封托管外联邮件）：第 1 封单步回复率最高（0.59%），但第 2–6 封合计贡献 58.6% 的回复；第 3 封贡献 35.6% 的邮件来源会议，比第 1、2 封加起来还多；建议序列 3–5 步，第 5 步后量"急剧下降"；**没有给出间隔天数数据**，也不涉及报价/会后跟进 — [Belkins：Sales follow-up statistics（厂商研究）](https://belkins.io/blog/sales-follow-up-statistics)
- 同一厂商 2025 版（2024 年 1650 万封）结论不同：单封邮件回复率最高（8.4%），每多一封都下降，5 封以上约减半，4 封以上的序列让退订和垃圾投诉翻三倍以上；2023 版又说第一次跟进让回复率 +49%、两次跟进最优。**同一来源三年三个结论**，说明这些数字高度依赖样本 — [Belkins 2025 study](https://belkins.io/blog/sales-follow-up-statistics?frame=0)（数字来自搜索摘要）
- Gong Labs 分析过 304,174 封跟进开发邮件（研究长度和措辞），本次未能取得具体结论 — [Gong](https://gong.io/?p=14740)

**回复速度（多为旧数据）**
- InsideSales/MIT Lead Response Management（**2007 年，旧**）：5 分钟内回应比 30 分钟后回应"合格线索"可能性高 21 倍 — 多为二手转引，见 [Guideflow 汇总](https://www.guideflow.com/blog/speed-to-lead)
- HBR《The Short Life of Online Sales Leads》（**2011 年，旧**）：1 小时内回应的公司，联系上决策人的可能性约为晚回应者的 7 倍 — [HBR 2011](https://hbr.org/2011/03/the-short-life-of-online-sales-leads)
- Workato 实测 114 家 B2B 公司（2022）：超过 99% 不在 5 分钟内回应，电话平均 14 小时 29 分 — [Workato](https://workato.com/the-connector/lead-response-time-study)；Drift（2017，旧）：433 家中仅 7% 在 5 分钟内回应 — [Guideflow 转引](https://www.guideflow.com/blog/speed-to-lead)
- 常被引用的"B2B 平均 42 小时"找不到原始出处 — [Guideflow](https://www.guideflow.com/blog/speed-to-lead)

**外贸从业者经验（观点，非数据）**
- 报价后：建议约 48 小时内做一次"轻触达"，不催单；两个误区是"报完价干等"和"第二天就问考虑得怎样" — [Zoho 中国知识库（厂商）](https://www.zoho.com.cn/books/articles/baojiajiqiao.html)；另一经验帖认为次日跟进合适，但别直接问价格、别问只能答"是/否"的问题，可用现货、交期缩短等新信息让客户主动回 — [AMZ123 问答](https://www.amz123.com/ask/1hDmPTYS)
- 同类文章里"超过 60% 外贸订单流失源于报价后跟进不当"来源不明，不建议引用 — [Zoho 中国](https://www.zoho.com.cn/books/articles/genjin.html)
- 寄样后：寄样后 1–2 周确认收到、30–45 天内跟进反馈、两次没回就转长期培育 — [搜狐](https://www.sohu.com/a/918972299_121668800)；[小满（厂商）](https://www.xiaoman.cn/article/250.html)（转引自上一轮报告）；样品反馈慢常因多部门审批/第三方测试/比价 — [Zoho 中国](https://www.zoho.com.cn/campaigns/articles/foreign-trade0428.html)
- 毅冰：跟进不是简单催促；供应商连续两个月几乎每天发空洞催促邮件，最后被拉黑 — [雨果网](https://www.cifnews.com/article/9431)（转引自上一轮报告）
- 中东：客户周五不办公、斋月期间"绝对不能催单"、爱发语音少看长邮件 — [孚盟（厂商）](https://www.fumasoft.com/news/4902.html)（转引自上一轮报告）
- 斋月（2026 年的商务简报）：建议把需要对方拍板的事**提前到斋月前 2–3 周**，开斋节后审批会集中涌来；斋月期间会议密度降低、政府手续变慢；沙特对穆斯林员工斋月工时限制为每天 6 小时/每周 36 小时 — [Middle East Briefing 2026](https://www.middleeastbriefing.com/news/ramadan-2026-in-the-uae-and-saudi-arabia-compliance/)
- 斋月期间商业活动集中在开斋饭后的深夜 — [DHL 2022（偏零售）](https://www.dhl.com/discover/en-my/business/market-intelligence/commerce-trends-ramadan-2022)
- 外贸财务视角：斋月期间中东客户工时缩短、银行和政府效率降低，付款审批变慢；开斋节等假期常持续一周以上，应收款这段时间基本不会到账，不应把这段逾期当异常 — [Zoho 中国：中东回款（厂商）](https://www.zoho.com.cn/books/articles/zhongdong0608.html)；物流文章建议货物尽量在斋月前到港 — [白鲸出海](https://www.baijing.cn/article/28113)
- 斋月日期每年提前约 11 天，且部分国家临时调整开斋节日期（如伊拉克 2025 年推迟），应以客户所在国官方公告为准 — [Al-Monitor 2025](https://www.al-monitor.com/zh-hans/originals/2025/03/tushuozhongdonggedemusilinhuanqingkaizhaijie)

### Inferences
- 对熟客跟进，"标准间隔"没有可靠数据支撑；更合理的是**事件驱动 + 阶段相关的默认窗口**：报价后 2–3 天无回音可提一次；寄样后先按物流时长确认签收，签收后留 1–2 周测试期再问反馈；PI 发出后关注定金；报价有效期到期前是天然的跟进理由。这些默认值应作为"候选进入窗口"，而不是到点必推。
- 冷开发数据里"第 3 次触达贡献最多会议"可以解释为什么人不该在一两次没回后放弃，但"4 次以上投诉翻三倍"同样提示：系统应记住"已经连续追了几次没回"，第 3 次后转为长期培育、降低频率。
- 时区和节假日应该影响"今天要不要把它放进列表"：对方周末（中东周五/周六）、斋月、开斋节、春节类假期期间，提醒应自动后移或换成"节后再问"，并在理由里写明。斋月前 2–3 周反而是**推动拍板的窗口**。
- "客户来信未回"是证据最强的一类：所有回复速度研究（虽然旧）方向一致，且它是事实不是判断。

### Gaps
- 没找到任何针对"报价后/寄样后/发 PI 后"的跟进间隔与成交率的量化研究（无论国内外）。
- 没找到 WhatsApp 场景下 B2B 跟进节奏的数据。
- Gong Labs 跟进邮件研究的具体结论未能取得。
- 福步论坛、知乎专栏的一手经验帖本次没有抓到原文，外贸部分主要来自厂商知识库和上一轮报告转引。

## 三、每日建议列表如何避免"提醒疲劳"

### Takeaway
跨领域的一致证据是：**提醒越多，越没人看**；医疗决策支持里医生普遍认为"提醒太多、大部分该删"。有效的设计特征是：每天封顶（Dex 5 条）、不打断工作（列表而非弹窗）、在自然断点出现（早上、任务间隙）、每条带具体上下文、能一键推迟/忽略且忽略不会变成逾期。"建议"必须和"承诺/待办"分开，并且会自然过期。

### Cited Findings
- 医疗用药提醒用户调查（JMIR 2020）：约 90% 回答的医生（30/33）认为提醒太多，58% 选"太多了，大部分该删"；30% 的医生"从不/很少"读提醒，有人描述"严重提醒疲劳"；24% 认为没有一类提醒有用；改进方向是更少、更具体、更不敏感的提醒，按严重程度区分，文字简短 — [Baysari 等，JMIR 2020;22(3):e14855](https://pmc.ncbi.nlm.nih.gov/articles/PMC7101499)
- 同一研究中，护士反而想要更多提醒（97% 想要给药提醒）——说明"要不要提醒"取决于角色和是否对应真实工作 — [同上](https://pmc.ncbi.nlm.nih.gov/articles/PMC7101499)
- 一项不打断工作流的肾损伤提醒（常驻显示而非弹窗），满意度 6 个月 83.3%、12 个月 94.4% — 来自搜索摘要，原文未核实（见 [PMC9908418](https://pmc.ncbi.nlm.nih.gov/articles/PMC9908418)）
- 主动式 AI 编程助手研究（CHI 2025，Pu 等）：18 人被试，主动式 AI 比纯提问式提高效率，但也打断工作；加上"AI 在做什么"的提示和交互上下文能减少打扰 — [arXiv 2502.18658](https://arxiv.org/abs/2502.18658)。全文数据（来自搜索摘要）：53.3% 的主动介入被有效采用，12.1% 造成打扰，34.7% 被忽略或没注意到；在**任务边界**介入最有效；而某类触发器产生大量"确认式"消息，导致用户忽略约 50% 的主动信号 — [arXiv PDF](https://arxiv.org/pdf/2502.18658)
- 15 名开发者 5 天实地研究（IUI 2026）：时机决定接受度，提交代码之后的建议比任务中途的更容易被接受 — [arXiv 2601.10253](https://arxiv.org/pdf/2601.10253v1)
- Dex：每天最多 5 条 keep-in-touch，批量设置后自动摊开；忽略的不累积，下周期再来 — [Dex 指南](https://guide.getdex.com/features/keep-in-touch)
- Pipedrive Sales Assistant 提供"今天暂停"和"每日汇总邮件"两种降噪方式 — [Pipedrive 帮助](https://support.pipedrive.com/nl/article/sales-assistant-notifications-beta)
- Close 的 Inbox 把"推迟"做成一等公民：推迟的条目进"未来"栏，到时间再回来 — [Close 帮助](https://help.close.com/docs/inbox)
- Gmail Nudges 被批评的点恰恰是"不可控"：不能告诉它哪些重要、手机上没有关闭按钮 — [Mailmeteor 2026（厂商博客）](https://mailmeteor.com/blog/gmail-nudges)
- Salesforce NBA 每条推荐都有接受/拒绝按钮并记录反应，可按策略统计 — [unofficialsf](https://unofficialsf.com/reporting-and-tracking-with-next-best-action/)
- Trosa 自己的教训：15/30/60 节点按日历生成，没人认领就留下约 1,190 条旧行，官方理由是"污染 Today"；代码里已用 SQL 强制"退役 outreach 调度行永不能转为待办" — [现状调研笔记](<../Trosa 产品方向深度调研/trosa_current_state.md>)

### Inferences（设计建议）
- **数量**：每人每天 3–7 条为宜（参考 Dex 封顶 5；四人团队、每人活跃客户几十到上百）。超出部分不显示"还有 23 条"，而是只留在"可以问助手"的池子里。客户来信未回不受封顶限制（它是事实，不是建议），单独一组置顶。
- **每条必须有三样东西**：①"为什么现在"——一句话、带具体事实和日期（"10-20 报价 3mm 透明板 2.1 USD/kg，之后客户没回，已 7 天"）；②"可以说什么"——一句开场建议或一键草稿，按毅冰的原则带新信息，而不是"请问考虑得怎样"；③原始消息链接——点开就是那条报价/那段对话。没有可链接的原始事实，就不生成这条建议（防止编理由）。
- **三个按钮，语义分清**：「已跟进/去跟进」（记录事实）、「改个日子」（推迟，变成人确认过的日期才算承诺）、「不用跟」（可选一个理由：已在别处处理 / 客户说了晚点再联系 / 不再合作 / 时机不对）。理由选项 3–4 个即可，跳过不选也行。
- **"建议 ≠ 承诺"**：建议只活当天（或最多 2–3 天），不处理就**静默消失**，不留逾期、不累积；系统第二天按新事实重新算，若理由仍成立可以再出现，但要降权（同一客户被忽略 2 次后，至少 N 天不再主动推）。只有人点了"改个日子"才变成待办。这直接解决 15/30/60 的债务问题。
- **自动作废**：只要该客户出现任何新沟通（任一成员、任一渠道），当天的建议立即失效，重新计算。这是避免"推荐已经处理过的客户"的关键。
- **从忽略里学习**：先用最笨的办法——记录每条建议的结果（采纳/推迟/不用跟+理由/无视），按"建议类型 × 阶段 × 人"统计；某类建议对某人采纳率长期很低，就降低其优先级或调长窗口。不需要训练模型。
- **出现时机**：早上一次性给出（自然断点），不弹窗、不推送；手机端只推"客户来信未回"这一类。
- **不排名、不对比同事**：只给关系主人本人看（延续上一轮报告的建议，避免"被监视"）。

### Gaps
- 没找到 CRM/销售场景下"每日建议条数 vs 采纳率"的研究，数量建议是借鉴 Dex 和医疗/编程领域的推断。
- 那条常被引用的"用药提醒覆盖率约 80%（55%–96%）"荟萃分析，本次未能打开原文确认出处，未采用。
- "从忽略中学习"在商业产品里的具体做法（folk、Pipedrive 是否学习）没有公开资料。

## 四、CRM 数据上的对话助手：怎么做才可信、才会被用

### Takeaway
成熟做法有四条：①回答要**落到具体记录**（Attio 在聊天里给记录链接和数据表，Day.ai 每个字段带来源和版本历史，Glean 引用受权限约束）；②没检索到就**不要装作有来源**（Glean 明说"没检索就没引用"）；③写入只能**生成提案、人点确认**；④最重要——**独立聊天窗口没人用，嵌在工作流里的才有人用**：微软给 6.2 万销售推 Copilot，半年都没起来，靠培训和习惯干预两年后才过 60% 日活；Salesforce 自己承认"创新跑在客户采用前面"。Trosa 的 Pi 聊天退役与此吻合。

### Cited Findings
- Ask Attio（2026-02 更新）：运行在 Claude Sonnet 4.6 和 Gemini 3.1 Pro 上；回答可在聊天内显示数据表和**紧凑的记录链接**；用了网页搜索时有图标，悬停可看来源；"再试一次"可要求更详细/更简洁；个人设置里可配隐私行为、默认模型、查看额度消耗；**页面未说明写入如何确认** — [Attio changelog 2026-02](https://attio.com/changelog/2026/ask-attio-updates)
- Ask Attio 可搜索记录、更新商机、起草跟进；会自己决定查日历、通话记录、邮件、记录甚至实时网页 — [ZoomInfo Pipeline 评测](https://pipeline.zoominfo.com/sales/attio-review)
- Ask Attio 还提供"实时提示建议"（如通话结束前回顾漏掉的内容）——即**在场景里给出可点的问题**，而不是空白输入框 — [Attio changelog 2026-02](https://attio.com/changelog/2026/ask-attio-updates)
- Day.ai：每个 CRM 字段都带来源和版本历史，用户能看到值为什么是这样、何时写入、引用了什么来源（这是字段层面，不是聊天回答层面）— [Parallel.ai 案例（与 Day 合写）](https://parallel.ai/ai/blog/case-study-day-ai)；独立分析未找到可验证的案例，建议买家做对照试点、以自动更新的准确率为标准 — [Airframe 分析](https://www.airframe.ai/product/day-ai/analysis)
- Glean：引用永远不会授予新权限，每人只看到自己有权看的来源；两种数据源都关闭时只用模型自身知识，**这类回答没有引用**；遇到"通用问题"时可能跳过检索、因此也没引用；"思考模式"检索更多、引用更可靠，"快速模式"可能跳过检索 — [Glean 文档：Citations](https://docs.glean.com/user-guide/assistant/glean-chat/glean-chat-citations/glean-citations)；[Glean in Gemini](https://docs.glean.com/administration/platform/embedded-integrations/glean-in-gemini-chat)
- 独立观点：点开引用能可靠发现"引错了"，但无法确认检索是否完整——"助手只展示它用了的来源，不展示它漏掉的" — [watts.it.com](https://watts.it.com/modules/grounded-answers-need-a-source-check)
- HubSpot Breeze Prospecting Agent 的所有外联都是"草稿交人审阅，审过才发" — [HubSpot 产品页](https://www.hubspot.com/products/sales/ai-prospecting-agent)
- Salesforce CRMArena-Pro（2025-06）：顶级 LLM agent 在 CRM 销售/服务/报价任务上单轮成功率约 58%，多轮降到约 35%；通用 agent 的保密意识"几乎为零" — [Salesforce 博客](https://www.salesforce.com/blog/crmarena-pro/)；[The Register](https://www.theregister.com/2025/06/16/salesforce_llm_agents_benchmark/)
- 采用难题：Dreamforce 2025 数据，过去一年约 12,500 家客户采用 Agentforce（不含 Slack 内 agent），其中付费约 6,000；Benioff 形容为"创新跑在客户采用前面" — [Fortune 2025-10](https://fortune.com/2025/10/21/salesforce-marc-benioff-ai-agents-dreamforce-agentforce-eye-on-ai)
- 微软内部给 62,000 人的销售组织推 Copilot（2024 年初），**6 个月后采用仍未起来**；两年后日活超 60%、月活超 98%，转折靠专项培训、同伴带头人和习惯养成干预；文中区分"嵌入式助手"与"自主 agent"，后者有不同的控制和接受问题 — [Let's Data Science 转述 HBR 案例（二手）](https://letsdatascience.com/news/microsoft-scales-copilot-adoption-in-sales-organization-a8136b93)
- Agentforce 使用率低的原因之一是员工缺乏"怎么提问、怎么和 AI 协作"的训练 — [Whatfix（数字采用厂商博客）](https://whatfix.com/blog/salesforce-agentforce-adoption/)
- Jakob Nielsen 的"表达障碍"：纯输入框式 AI 要求用户能写清楚意图，他估计富裕国家约一半人做不好；建议用"相关问题""提示构建器""参数化"等方式代替空白输入框，并认为语音输入能部分缓解 — [UX Tigers：Articulation Barrier](https://www.uxtigers.com/post/ai-articulation-barrier)；[Prompt Augmentation](https://www.uxtigers.com/post/prompt-augmentation)（个人专栏，非 NN/g 正式报告）
- Gong 自述：使用"Ask Anything"（对单子问问题）的团队赢单率高 26%——厂商自研，有选择偏差 — [VentureBeat](https://venturebeat.com/ai/how-gongs-ai-tools-are-increasing-win-rates-for-sales-teams)
- Trosa 自己：Pi 聊天 2026-08-31 上线、只绑定 Hamid 一人、部署修复多次，现结论"不恢复独立 Pi 前端运行时；保留的 `/api/agent/*` 只能做原子读取和确认式提案" — [现状调研笔记](<../Trosa 产品方向深度调研/trosa_current_state.md>)

### Inferences（设计建议）
- **入口不要是"助手页"**，而是三处现成位置：①每条每日建议上的"问一下"（预填"为什么建议跟进 X？上次说到哪？"）；②客户页顶部一个输入框，预置 2–3 个可点问题（"现在适合跟进吗？""上次报价是多少？""答应过客户什么？"）；③"今天"页底部一句"还想看看谁？"——用来回答老板那种"我想到某客户是不是也可以跟进了"。都在已有页面里，不新增一级页面。
- **"可以跟进了吗？"的回答模板**（固定结构，便于信任）：结论（可以/建议再等/不建议）+ 依据 2–3 条（每条带日期和指向原始记录的链接）+ 若建议跟进，一句话开场建议 + 按钮「记下 X 日跟进」「起草消息」。判断依据只能来自时间线里能链接的事实；时间线里没有的（如客户 WhatsApp 里说过的话没录进来）要明说"系统里最后一条记录是 X 日，之后的情况我看不到"。
- **说"不知道"的规则**：没检索到记录、匹配到多个同名客户、最后记录太久远时，必须直接说并给出澄清选项（"你说的是 A 公司还是 B 公司？"），而不是猜。CRMArena-Pro 35% 的多轮成功率说明不能让它串多步推理后下结论。
- **写入只给按钮**：助手可以生成"记一个跟进日期""写一条沟通记录""起草回复"的提案，人点确认才写，写入保留来源与撤销（与现有 `/api/agent/*` 确认式提案约定一致）。
- **延迟**：建议列表应**每天凌晨预先算好**，打开"今天"页零等待（核心页面不能等 AI，见 AGENTS.md）；对话回答可以几秒，但要先显示"正在查 X 客户的 N 条记录"之类的进度，避免空等。（Superhuman 把速度当头号价值，见上一轮报告。）
- **语音优先**：老板和同事习惯 WhatsApp 语音，Nielsen 的表达障碍论点支持在手机上提供按住说话提问。
- **权限**：助手的检索范围 = 提问人本来就能看到的范围（Glean 的做法），跨成员只返回白名单字段（与 Trosa 现有规则一致）。

### Gaps
- 没找到 Ask Attio、Breeze Assistant、Agentforce 在"写入前确认"上的具体交互说明。
- 没找到对话助手在 CRM 场景下"说不知道"的设计文档或评测。
- 没找到对话式 CRM 助手的延迟基准或用户可接受延迟的研究。
- 微软 Copilot 案例为二手转述，HBR 原文未核实。

## 五、失败模式、信任与衡量方法

### Takeaway
最伤信任的四种失败：**编造理由**（说客户"上次问了价"其实没有）、**推荐已处理的客户**（别的渠道/别的同事刚聊过）、**误报逾期**、**数量过多**。防御靠结构而不是提示词：理由只能引用可链接的事实、任何新沟通立即作废建议、建议不累积。衡量上，只看"被看到的建议里有多少被采纳并真的跟进了"，以及"采纳后客户回复的比例"，并对照"系统没建议、人自己跟进的"作为漏报信号。

### Cited Findings
- 误报逾期的真实例子：Pipedrive 用户报告潜客被重复显示为"逾期"，而跟进其实排到了 2026 年 — [G2 评论](https://www.g2.com/products/pipedrive/reviews/pipedrive-review-12107342)
- 自动捕获的匹配错误（抄送重复计数、换邮箱后线程脱钩）是最常见失败点之一，会直接导致"系统以为没跟进其实跟进了" — [Revenue Grid（竞品撰写）](https://revenuegrid.com/blog/einstein-activity-capture-limitations/)（转引自上一轮笔记）
- LLM agent 多轮 CRM 任务成功率约 35%，保密意识近零 — [Salesforce CRMArena-Pro](https://www.salesforce.com/blog/crmarena-pro/)
- 引用只能证明"用了的来源是对的"，不能证明没漏掉 — [watts.it.com](https://watts.it.com/modules/grounded-answers-need-a-source-check)
- Breeze 补全数据过时/错误、草稿像机器人，是 AI 助手最直接的信任损耗 — [LowCode Agency](https://www.lowcode.agency/blog/hubspot-breeze-ai-reliability-problems)
- 过度催促的代价：外贸场景里"每天空洞催促"被客户拉黑 — [雨果网](https://www.cifnews.com/article/9431)；冷开发里 4 封以上让退订/投诉翻三倍 — [Belkins](https://belkins.io/blog/sales-follow-up-statistics?frame=0)
- 衡量机制参考：Salesforce NBA 记录每条推荐的接受/拒绝并按策略汇总 — [unofficialsf](https://unofficialsf.com/reporting-and-tracking-with-next-best-action/)；主动式 AI 研究用"有效采用 / 打扰 / 忽略"三分法（53.3% / 12.1% / 34.7%）— [arXiv 2502.18658](https://arxiv.org/pdf/2502.18658)
- 隐私：国内用户对小满的疑虑之一是"担心客户数据被阿里使用" — [PingCode 汇总](https://docs.pingcode.com/ask/64059.html)（转引自上一轮报告）；Attio 默认只共享时间/参与人/主题、正文私有 — [Attio 帮助](https://attio.com/help/reference/email-calendar/email-and-calendar-syncing)
- Trosa 内部提醒：contact_state / next_task_date 等派生口径不可信，判断要读时间线 — 用户记忆 `trosa-contact-state-unreliable`（内部）

### Inferences（防御与指标）
- **防编造**：每条建议的理由由**模板 + 结构化事实**拼成（事件类型、日期、金额/规格、距今天数），LLM 只负责把它说顺和起草开场白；生成后做一次校验——理由里出现的日期/金额必须能在被链接的记录里找到，找不到就丢弃该条。
- **防已处理**：计算前合并所有成员、所有渠道的最新沟通（含 Sela 交接的回信）；客户最后一条消息若来自客户 → 归"待回复"组而非"跟进"组；最近 N 天内有我方沟通 → 不建议。建议显示时再实时复核一次。
- **防"系统看不见"**：WhatsApp/微信未录入是最大盲区。理由里写"系统里最后一条是 X 日"，按钮里放「其实已经聊过」——点了就作废并顺手记一条简短沟通（既修正建议，又补了数据）。
- **防过度依赖**：建议只是"候选"，界面用语避免"必须/逾期"；助手结论用"建议"语气，并总是展示依据。
- **指标（四人团队够用的最小集）**：
  1. 采纳率 = 点了"去跟进/改日子"的条数 ÷ 被展示的条数（按类型、按人看）；
  2. 落实率 = 采纳后 3 天内真有对外沟通记录的比例；
  3. 回复率 = 落实后 14 天内客户有回复的比例（对照：人自己发起、非系统建议的跟进）；
  4. "不用跟"理由分布 —— "已在别处处理"多 = 数据盲区；"时机不对"多 = 窗口太短；
  5. 漏报信号 = 人自己跟进、且客户回复了，但系统前 7 天没建议过的客户数；
  6. 无视率（看到但没点任何按钮）持续上升 = 数量太多或理由不准，先减量。
  样本小，不追求统计显著，每月看一次趋势、问一句"哪条建议最没用"。

### Gaps
- 没找到任何 CRM 厂商公开的"AI 建议采纳率基准"，无法给出"多少算好"的外部参照；上面的主动式编程助手 53% 有效采用率领域不同，仅供参考。
- 没找到关于 AI 跟进建议导致"过度依赖"的实证研究。
- "建议过期天数""同客户忽略后静默多久"没有外部数据，需要在 Trosa 内部试运行后调参。
