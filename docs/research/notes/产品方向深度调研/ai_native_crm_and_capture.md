# AI 原生 CRM 与沟通自动捕获（WhatsApp / 邮件 / 微信）调研笔记

调研日期：2026-10-27。资料以 2025–2026 为主，较旧的已标注。大部分资料来自厂商自己的博客或竞品对比页，可信度有限，文中已逐条注明。整体结论先放这里：**各家都在卖“CRM 自动更新”，但我没找到任何一份独立测量的“自动捕获/自动写入准确率”数据；唯一像样的独立基准（Salesforce 自己的 CRMArena-Pro）显示多步骤任务成功率只有约 35%。**

---

## 问题 1：AI 原生 CRM 和销售助手现在到底自动化了什么？真实用户对准确性和信任怎么说？

### Takeaway
新一代产品（Lightfield、Day.ai、Clarify、Attio）的共同卖点是“从邮件/日历/会议录音自动生成客户记录，不用手填”，但它们几乎都只接 Gmail/Outlook + 日历 + 视频会议，**不接 WhatsApp 和微信**；准确率数据全是厂商自报。老牌 CRM（HubSpot Breeze、Salesforce Agentforce、Pipedrive）的 AI 多数还停在“摘要、草稿、建议”，用户抱怨主要是数据过时和文风生硬。

### Cited Findings
**AI 原生 CRM（2025–2026 新玩家）**
- Lightfield：为每个“捕获席位”同步 Google/Outlook 邮件和日历，录制并转写会议，再根据谈话内容更新记录；定位是“不用手动维护的 CRM、公司共享记忆”。价格 Starter $89/用户/月，Pro $999/月/工作区，额度未公开 — [efficient.app Lightfield Review 2026](https://efficient.app/apps/lightfield)；[Ahoy 对比（竞品撰写）](https://ahoy.ai/comparisons/best-ai-native-crms/)
- Lightfield 2025 年 11 月上线，获 a16z 领投 4700 万美元 A 轮，自称已有 5000+ 公司客户；它持续采集 email、日历、Slack、LinkedIn 的互动 — [Pulse 2.0](https://pulse2.com/lightfield-raises-47-million-series-a-to-build-ai-native-crm-for-companies-using-agents/)
- Lightfield CEO 的说法是：AI agent 在 CRM 上失败的原因是数据本身“不完整、不准确、缺乏结构”，不是模型不够强；它对外宣传的“95%+ recall”之类数字是公司自报，而且相关 Inc./Tech Times 报道是付费投放 — [AuthorityTech（Lightfield 公关方）](https://authoritytech.io/results/lightfield-inc-crm-data-ai-agents-failing)
- Product Hunt 上有评论指出：即使数据都在，团队也不一定信它、不一定拿它做预测 — 同上来源转述（[hunted.space](https://hunted.space/product/lightfield)）
- Day.ai：记录不是人填的，而是“从你的沟通里自动生成”，本质是一张可以用对话方式查询的关系图，数据来自邮件、日历、会议；批评者说它的销售流程/CRM 深度偏弱，会议转写和总结强，但团队协作和数据统一弱 — [Coffee.ai: Day.ai vs Attio（竞品撰写）](https://www.coffee.ai/articles/day-ai-vs-attio-review)；[Dreamhub 对比（竞品撰写）](https://www.dreamhub.ai/blogs/best-ai-native-crms-2026-honest-comparison-attio-clarify-day-ai-reevo-dreamhub)
- Attio：数据模型最灵活，AI 功能是真的，但有竞品认为它的记录“大部分仍要手填”；另一来源则说它有邮件/日历自动捕获。来源之间互相矛盾 — [Dreamhub](https://www.dreamhub.ai/blogs/best-ai-native-crms-2026-honest-comparison-attio-clarify-day-ai-reevo-dreamhub)；[wetheflywheel](https://wetheflywheel.com/en/guides/best-ai-native-crm-2026/)
- Clarify：自称“自主 CRM”，自动录制会议和电话，转为结构化笔记，并标出要点、风险和下一步 — [folk: folk vs Clarify（竞品撰写）](https://www.folk.app/articles/folk-CRM-vs-Clarify-AI)
- folk：会后 AI 生成结构化摘要、抽取行动项、起草跟进邮件、建议下一步；起价 $20/用户/月 — [folk 官方博客](https://www.folk.app/zh/articles/crm-with-ai-meeting-recap-and-notes)

**老牌 CRM 的 AI**
- Pipedrive：AI 邮件线程摘要（2024-11 文档，较旧），当时为 Beta，只在 Professional 以上套餐，需要接受 AI Beta 条款；原生没有会议录音，靠第三方插件（如 Flowmeets）把通话摘要写进联系人、生成活动 — [Pipedrive 帮助中心](https://support.pipedrive.com/article/ai-email-summarization)；[Pipedrive Marketplace: Flowmeets](https://www.pipedrive.com/en/marketplace/app/flowmeets/e39bd922231d3185)
- HubSpot Breeze：多方反馈 Breeze Intelligence 给出的公司信息过时或错误，尤其对小企业和小众行业；Reddit 上有人吐槽 Breeze 写的邮件“像一个努力装成人的公司机器人”；咨询公司评价它“营销很猛，可靠性不稳定” — [LowCode Agency（咨询方博客）](https://www.lowcode.agency/blog/hubspot-breeze-ai-reliability-problems)；[eesel.ai（竞品撰写）](https://www.eesel.ai/blog/breeze-agent-analytics)
- 代理商建议：其他 Breeze agent 都继承 CRM 里现有数据的质量，Beta 阶段的 agent“当作有前途的东西，不要当生产工具” — [InsideA](https://insidea.com/blog/hubspot/which-hubspot-breeze-agent)
- Salesforce Einstein Activity Capture（老牌自动记录）：数据存在 AWS 而非 Salesforce 本身，标准报表用不了；Standard 版只保留 6 个月、Full 版默认 24 个月后删除；同步延迟从几分钟到几小时；匹配会出错（如 CC 的邮件重复计数）；会议只记“开了会、谁参加”，没有内容和行动项 — [Revenue Grid（竞品撰写）](https://revenuegrid.com/blog/einstein-activity-capture-limitations/)；[Avoma（竞品撰写）](https://www.avoma.com/blog/salesforce-activity-capture)

**独立基准（最重要的一条）**
- Salesforce AI Research 的 CRMArena-Pro（2025-06）：顶级 LLM agent 在 CRM 销售/服务/报价类任务上单轮成功率约 58%，多轮降到约 35%；最容易的“执行固定流程”单轮可到 83%+（Gemini 2.5 Pro）；所有通用 agent 的保密意识“几乎为零”，靠提示词能改善但会损失任务表现 — [The Register](https://www.theregister.com/2025/06/16/salesforce_llm_agents_benchmark/)；[Salesforce 官方博客](https://www.salesforce.com/blog/crmarena-pro/)

**中国外贸 SaaS：OKKI / 小满**
- 小满科技（阿里国际站旗下）给 OKKI 的营销获客、客户管理、经营分析都加了 AI：AI 开发信、多语言翻译、智能数据分析、跨时差自动回复、按历史聊天总结要点 — [界面新闻](https://m.jiemian.com/article/9485001_sina.html)
- 2025-06 推出外贸营销智能体 OKKI AiReach，内测称开信率从 6% 提到 15%、询盘增长 3 倍（厂商内测数据，未经独立验证） — [扬子晚报网](https://www.yzwb.net/news/ch/202506/t20250612_220612.html)

### Inferences
- 美国这批 AI 原生 CRM 的“自动捕获”都建立在邮件+日历+视频会议上，这是美国 SaaS 销售的工作方式；**Trosa 的主战场（WhatsApp、微信、语音、图片报价单）它们基本不覆盖**，不能直接照抄它们的“全自动更新”承诺。
- 厂商自己也承认问题在“数据不准”，而它们的解法是“更多自动捕获”；但 CRMArena-Pro 的数字说明，让 AI 跨多步自动改业务字段（阶段、日期、价格）错误率仍然很高。适合自动做的是**单步、可回看原文的事情**（摘要、抽取、草稿），不适合的是**多步推理后直接改状态**。
- 用户的信任问题集中在两点：AI 写错了没人发现（数据过时）、AI 写的文字不像人。这正好对应 Trosa 现有“AI 只读 + 人确认”原则的价值，前进方向应是“减少打字”，而不是“减少确认”。

### Gaps
- 没找到任何一家（Lightfield、Day.ai、Attio、Clarify、Breeze、Agentforce）由第三方独立测量的自动记录准确率；Reddit/HN 上的一手用户讨论本次没能直接抓到，只有二手转述。
- Agentforce 面向中小团队的真实使用反馈本次没找到。
- OKKI 是否原生接入 WhatsApp、准确率如何，搜索结果没有覆盖，需看 OKKI 官网。

---

## 问题 2：WhatsApp / 微信 / 邮件 的自动捕获有哪些路，各自成本、限制、封号/合规风险是什么？

### Takeaway
三个渠道里，**邮件最容易也最稳**；**WhatsApp 只有“官方 API”是零封号风险，但它要求是商业号，2025 年起的 Coexistence（共存）模式让“手机上继续用 WhatsApp Business App + 后台同步”成为可能**，这是对 4 人团队最有意义的新变化；用个人号 + 网页版插件/非官方库的方案违反条款、有不可预测的封号风险。**微信个人号没有合规的自动存档途径**，只有企业微信的“会话内容存档”可以官方拉取，但需要客户在企业微信体系里、要付费、要自己开发解密。

### Cited Findings
**WhatsApp 官方 API（Cloud API）**
- 2025-07-01 起 Meta 改为按条计费：模板消息逐条收费，取代原来的“24 小时会话”计费 — [Meta 官方定价页](https://developers.facebook.com/docs/whatsapp/pricing)
- 客户先发消息后开启 24 小时“客服窗口”（从客户最后一条消息起算）；窗口内的自由文本消息免费，窗口内发的 utility 模板也免费；窗口外主动联系客户必须用付费模板，价格按收件人国家和类别不同，可能按季度调整 — [Meta 官方定价页](https://developers.facebook.com/docs/whatsapp/pricing)；[Gallabox 文档](https://docs.gallabox.com/pricing-and-billing-modules/new-per-message-pricing-effective-july-1-2025)
- 二手来源称：以前每月 1000 条免费服务会话，2025-07 后服务会话不限量免费（官方表述是“窗口内自由消息免费”，并非数量额度） — [Wati 帮助中心](https://support.wati.io/en/articles/11561662-message-based-pricing-all-you-need-to-know)
- 通过 Click-to-WhatsApp 广告或 Facebook 主页按钮进来的客户，72 小时内所有消息（含模板）免费 — [Meta 官方定价页](https://developers.facebook.com/docs/whatsapp/pricing)
- 政策：自 2026-01-15 起，WhatsApp Business API 禁止“通用 AI 助手”（AI 本身是核心产品的情况，如 ChatGPT、Perplexity）；用于客服、预约、订单、通知、销售的业务机器人仍允许 — [respond.io](https://respond.io/blog/whatsapp-general-purpose-chatbots-ban)；[Gulf News](https://gulfnews.com/technology/no-more-chatgpt-and-perplexity-on-whatsapp-meta-bans-major-ai-chatbots-from-2026-1.500316016)

**WhatsApp Coexistence（同一个号码同时用 Business App 和 Cloud API）**
- 同一号码可以手机上继续用 WhatsApp Business App，同时接入 Cloud API；双向通过 “message echoes” 同步，手机上发的消息也会出现在 API 的会话历史里 — [360dialog 文档](https://docs.360dialog.com/docs/resources/phone-numbers/coexistence)；[Telnyx 发布说明](https://telnyx.com/release-notes/whatsapp-coexistence)
- 接入时可选择同步最多 6 个月的单聊历史（也可跳过）；媒体文件只同步最近 2 周 — [Telnyx](https://telnyx.com/release-notes/whatsapp-coexistence)；[Wati 博客](https://www.wati.io/en/blog/whatsapp-coexistence-solution/)
- 前提：Business App 版本 ≥ 2.24.17；只有在 Business App 上注册的号码可以用（直接注册在 Cloud API 上的号不行）；手机 App 需要定期打开（来源说法 13 天或 14 天一次，不一致）；接入后广播列表失效，联系人只单向同步 — [Wati](https://www.wati.io/en/blog/whatsapp-coexistence-solution/)；[Chatwerk 文档](https://developer.chatwerk.de/docs/whatsapp-coexistence-business-app-api)
- 以上大部分来自 BSP（服务商）文档，不是 Meta 官方页面，具体限制以 Meta 开发者文档为准 — 同上

**WhatsApp 个人号 / 网页版插件 / 非官方库**
- 用 whatsapp-web.js 或 Baileys 驱动个人号违反 WhatsApp 条款，存在“不确定的”封号风险；whatsapp-web.js 自己声明“不保证不会被封”，Baileys 声明与 WhatsApp 无关、不鼓励批量/自动化使用；封号似乎由机器学习判断，最重要的信号是“发给谁、被拉黑/举报的频率”，发送量本身没那么关键 — [checkleaked 开发者指南（有商业立场）](https://whatsapp.checkleaked.cc/blog/avoid-whatsapp-ban)；[zylos.ai 2026-01 研究笔记](https://zylos.ai/research/2026-01-26-whatsapp-api-automation)
- 竞品（Zenvia）把“WhatsApp 网页版插件”评为临时或永久封号高风险（有利益冲突） — [Zenvia](https://zenvia.com/blog/wa-web-plus/)
- TimelinesAI 有 Chrome 插件（在网页上识别电话号码、侧栏显示相关 WhatsApp 聊天），其 Zoho 集成自称“无需浏览器插件、不依赖手机”；它现在也在推 Coexistence 接入指南 — [Zoho Marketplace](https://marketplace.zoho.com/app/crm/whatsapp-extension-for-zoho-crm-by-timelinesai)；[TimelinesAI Coexistence 指南](https://timelines.ai/whatsapp-coexistence-account-setup-guide)
- 有扫描站点把另一款 WhatsApp CRM 插件标为可能恶意（与 TimelinesAI 无关），说明这类插件本身也有数据安全风险 — [ExtensionAuditor](https://extensionauditor.com/scan/wa-ojcllaigkeajeghbikbdanpoegecfikj)

**企业微信 会话内容存档**
- 官方定位：用于客服质量、内部协作和监管合规；企业统一设置存档员工范围，通过 API 获取“已开启存档员工”的工作沟通内容 — [企业微信开发者文档 91360](https://developer.work.weixin.qq.com/document/path/91360)
- 只能看到员工在**企业微信**上与客户互发的消息，员工**个人微信**的聊天不在范围内 — [头条转述](https://www.toutiao.com/zixun/7495206466526578738/)
- 官方只给接口，要二次开发；加解密复杂，官方 SDK 只有 Linux/Windows 的 C 和 Java 版本 — [头条话题](https://www.toutiao.com/topic/7531202112882083855/)；[gitee wework-msgaudit](https://gitee.com/worksmart/wework-msgaudit)
- 费用（2023 年前后资料，较旧）：一家中小企业账单里会话存档 6000 元/年，加人头费 2000 元、认证 300 元/年、CRM 4000 元，共约 1.23 万元/年；外部联系人 2000 人以内免费，超出按梯度收费（约 0.1 元/人/年） — [人人都是产品经理](https://www.woshipm.com/?p=5792411)；[投资界](https://news.pedaily.cn/202306/515561.shtml)
- 官方文档导航里有“客户同意进行聊天内容存档事件回调”页面，说明存在“客户同意存档”的流程，但正文本次未能抓取 — [企业微信开发者文档 91360](https://developer.work.weixin.qq.com/document/path/91360)

**邮件（Gmail API / IMAP）**
- `gmail.readonly`、`gmail.modify` 等属于 Google “受限范围”，公开应用需要 CASA 安全评估；成本说法差异很大：2019 年报道 1.5–7.5 万美元/年（较旧），2025 年审计机构称几百到几千美元/年，2026 年一名独立开发者 Tier 2 花了约 540 美元；`gmail.send` 只是“敏感范围” — [The Register 2019](https://www.theregister.com/2019/02/11/google_gmail_developer/)；[DeepStrike 2025](https://deepstrike.io/blog/google-casa-security-assessment-2025)；[Indie Hackers](https://www.indiehackers.com/product/mailnotes)；[Google Apps Script 社区](https://groups.google.com/g/google-apps-script-community/c/-A00Ngimg1A)
- 2019 年报道称 G Suite（Workspace）内部应用不适用该评估（较旧，未找到 Google 当前文档确认） — [The Register 2019](https://www.theregister.com/2019/02/11/google_gmail_developer/)

### Inferences
- 对 Trosa：**邮件**——团队自己的邮箱，用 IMAP + 应用专用密码，或 Google Cloud 项目设为“内部/测试”只给 4 个成员授权，就不需要走公开应用审核；这是最低风险、最先能做好的自动捕获渠道。（注：AGENTS.md 说明目前客户回信由 Sela 读邮箱后交接，Trosa 自带 gmail_sync 已停用，可沿用这条链路。）
- **WhatsApp**：如果销售们用的是 WhatsApp Business App（而不是纯个人 WhatsApp），Coexistence 是目前唯一既合规又能把手机上的真实对话同步进 CRM 的办法，且接入时能回补 6 个月历史；入站对话和窗口内回复免费，被动接收几乎零消息费，成本主要是 BSP 月费（或直接接 Meta Cloud API 自己开发）。纯个人号 + 非官方库/网页插件不建议用于长期存档：违反条款、封号不可预测，一旦封号丢的是业务主号。
- 手动兜底：WhatsApp 自带“导出聊天”（.txt + 媒体）可以作为单个客户的补录入口，交给 AI 整理成时间线候选；无封号风险，但需要人每次操作。
- **微信**：客户多在个人微信上的话，没有合规的自动拉取方式；企业微信会话存档成本（开发 + 每年数千元）对 4 人团队偏高，而且只覆盖企业微信。务实做法是“截图/转发/粘贴 → AI 整理 → 人确认”。
- Meta 2026 禁“通用 AI 助手”的规定不影响“CRM 读取自家业务对话做整理”，但如果以后想做“AI 自动回复客户”，必须是有明确业务范围的机器人。

### Gaps
- Coexistence 的 Meta 官方文档本次未直接打开，6 个月历史/13 或 14 天打开一次等限制来自服务商文档，需核对。
- 未确认：Coexistence 是否同步群聊（多个来源只说“individual chat history”，推测群聊不同步）。
- WhatsApp 各国（印尼、印度、中东、美洲）的模板单价本次没逐一列出，需查 Meta 当期价格表。
- 企业微信会话存档的现行官方价格、客户同意机制的具体规则（91361/91552 页面抓取失败）未核实；《个人信息保护法》对存档告知同意的要求未查。
- WhatsApp “导出聊天”的条数上限本次未核实。
- 没找到 respond.io、WAWCRM 等工具在个人号上的真实封号统计。

---

## 问题 3：“AI 提议、人确认”怎样做才不累？什么时候可以自动写入，什么时候有害？

### Takeaway
本次没有找到关于“一键确认 / 批量审阅 / 置信度阈值”的独立用户研究；能找到的证据都指向同一结论：**自动记录“发生了什么”（日志、原文）问题不大，自动改“业务判断”（阶段、日期、承诺、公司信息）容易出错且出错后没人发现**。

### Cited Findings
- Salesforce 旧的手动 Gmail/Outlook 记录插件因为“忘记记录、频繁重新授权”被销售放弃；某竞品称人工记录只捕获不到 28% 的邮件和会议（竞品营销数字，未核实） — [Avoma](https://www.avoma.com/blog/salesforce-activity-capture)
- 即使是全自动的 Einstein Activity Capture，也存在匹配错误、CC 重复计数、同步延迟几分钟到几小时的问题 — [Revenue Grid](https://revenuegrid.com/blog/einstein-activity-capture-limitations/)
- 多步骤 CRM 任务 AI 成功率约 35%，单步约 58%，固定流程执行可 83%+ — [The Register](https://www.theregister.com/2025/06/16/salesforce_llm_agents_benchmark/)
- 代理商建议 HubSpot Breeze 的 Beta agent“当作有前途的东西，不要当生产工具”，因为它们继承 CRM 已有数据的错误 — [InsideA](https://insidea.com/blog/hubspot/which-hubspot-breeze-agent)
- Breeze 自动补全的公司信息常常过时或错误，小企业/小众行业尤其明显 — [LowCode Agency](https://www.lowcode.agency/blog/hubspot-breeze-ai-reliability-problems)
- Pipedrive 启用 AI 邮件摘要需要接受 Beta 条款，**其他用户也要同意**（2024 年资料，较旧） — [Pipedrive 帮助中心](https://support.pipedrive.com/article/ai-email-summarization)
- 客户/联系人自动匹配错误是自动捕获最常见的失败点之一（EAC 匹配误判） — [Revenue Grid](https://revenuegrid.com/blog/einstein-activity-capture-limitations/)

### Inferences
- 适合**自动写入**（不需要确认）的：原始消息/邮件本身进入时间线（是事实，不是判断）、按唯一精确邮箱/手机号匹配到已有客户（Trosa 现有规则）、AI 摘要作为“附在原文旁的阅读辅助”且标明是 AI 生成。
- 必须**人确认**的：新建客户/联系人、待办和日期、价格/规格/交期等承诺、客户阶段变化、对外发出的任何消息。这些都是“判断”，且 CRMArena-Pro 显示多步推理错误率高。
- 低摩擦确认的做法（来自产品经验推断，非本次文献证据）：每条建议旁边直接显示原文那一句（证据），一个按钮确认、一个按钮忽略；同类建议攒成“今天待确认 N 条”在 Today/Inbox 批量过；高置信（原文里明确写了日期和动作）默认勾选、低置信默认不勾选；确认后可撤销。
- 判断“自动是否可接受”的简单标准：**错了会不会有人发现、发现后能不能一键撤回**。日志类错了容易看出来；日期/价格类错了往往等客户投诉才发现，必须确认。

### Gaps
- 没找到关于确认率、置信度阈值、批量审阅对采用率影响的独立研究或公开数据（Day.ai、Lightfield 是否提供审阅队列也未确认）。
- 没找到关于“AI 自动写入导致业务事故”的公开案例。

---

## 问题 4：对外贸小团队真正有价值的 AI 用法有哪些？

### Takeaway
最被验证的价值是“读”：翻译、摘要、抽取（价格/交期/规格/承诺）、回复草稿、会前客户速览。中国外贸 SaaS（OKKI）押注的是翻译、开发信、跨时差回复和聊天要点总结；美国产品押注的是会议录音转写与跟进草稿。移动端现场/电话录音已有成熟产品（Granola iPhone、Fireflies 移动端）。

### Cited Findings
- OKKI 的 AI 能力：多语言翻译、AI 开发信、跨时差自动回复、按历史聊天推荐/总结要点、经营数据分析 — [界面新闻](https://m.jiemian.com/article/9485001_sina.html)；[ai-bot.cn 介绍（可信度一般）](https://ai-bot.cn/okki/)
- folk/Clarify：会后摘要、行动项、风险、下一步，起草跟进邮件 — [folk](https://www.folk.app/zh/articles/crm-with-ai-meeting-recap-and-notes)；[folk vs Clarify](https://www.folk.app/articles/folk-CRM-vs-Clarify-AI)
- Day.ai 最强的是会议转写和总结 — [Coffee.ai](https://www.coffee.ai/articles/day-ai-crm-reviews-comparisons/)
- Granola iPhone App（2025-05 上线）可以记录线下会议和电话、识别说话人、同步桌面；但 iOS 不允许捕获其他 App 的音频，所以手机上无法转写 Zoom/Meet；未确认 iPhone 端能直接同步到 CRM — [Granola 官方帮助](https://docs.granola.ai/help-center/ios/getting-started)；[Circleback 对比](https://circleback.ai/compare/fireflies-ai-vs-granola.md)
- Fireflies 有移动端线下录音并能接 CRM — [Circleback](https://circleback.ai/compare/firefliesai-vs-granola)
- Breeze 写的邮件被吐槽“像机器人”——AI 草稿的语气是用户最直接感知的痛点 — [LowCode Agency](https://www.lowcode.agency/blog/hubspot-breeze-ai-reliability-problems)
- OKKI AiReach 内测称开信率 6%→15%（厂商自报） — [扬子晚报网](https://www.yzwb.net/news/ch/202506/t20250612_220612.html)

### Inferences
按对 Trosa 的价值和风险排序（推断）：
1. **“上次报了什么价、答应了什么”**：从时间线原文抽取价格、规格（厚度、颜色加价）、交期、付款条件，显示时附原文出处。价值高、单步任务、错了能对照原文，适合先做。
2. **客户速览（打电话/回消息前）**：几行总结最近沟通 + 未完成承诺。纯阅读，不写数据，风险低。
3. **回复草稿 + 翻译**：用客户的语言写草稿，人改后自己发出。注意语气（Breeze 的教训），草稿应模仿成员自己过去的写法而不是通用商务腔；不自动发送。
4. **从聊天中提议待办和日期**：“客户说下周三确认样品” → 提议待办，必须人确认（日期类错误后果大）。
5. **每日简报 / 沉睡客户提醒**：基于时间线计算，不需要强 AI；AI 只负责写一句“为什么现在该联系”。注意 MEMORY 里提到 contact_state/next_task_date 不可信，应从时间线原文推算。
6. **语音备忘 / 现场会议录音**：手机录音 → 转写 → 摘要进时间线候选；可以先接受用户把录音或转写文本贴进来，不必自建录音 App。
7. **自动回复客户**：价值存疑、风险最高（承诺价格/交期），且 WhatsApp 有 AI 机器人政策约束，不建议。

### Gaps
- 没找到独立数据证明 AI 翻译/草稿对外贸回复率或成交率的影响（只有 OKKI 自报的开信率）。
- 没找到针对印尼语、阿拉伯语、西语等外贸常见语言的 LLM 翻译质量独立评测。
- 语音消息（WhatsApp 语音条）转写的准确率与成本未查。

---

## 问题 5：自建 Flask/PostgreSQL CRM 调用 LLM API 的成本、延迟、隐私

### Takeaway
对 4 人团队，按量计费的 LLM 调用成本很低（每月大约几美元到几十美元量级，属推算）；真正的成本在于捕获渠道（WhatsApp BSP、企业微信存档）和开发维护。隐私上的主要风险是把客户对话发给第三方模型和插件服务商，以及 AI agent 的“保密意识几乎为零”。

### Cited Findings
- 小模型价格（第三方汇总，需以官方为准）：Claude Haiku 4.5 输入 $1.00 / 输出 $5.00 每百万 token；Gemini 2.5 Flash 输入 $0.30 / 输出 $2.50 每百万 token — [anotherwrapper LLM pricing](https://anotherwrapper.com/tools/llm-pricing/claude-haiku-4-5-20251001/gemini-2.5-flash)；[llmreference](https://www.llmreference.com/compare/claude-haiku-4-5/gemini-2.5-flash)
- 通用 LLM agent 在 CRM 场景里保密意识接近零，靠提示词改善会牺牲任务表现 — [Salesforce CRMArena-Pro 博客](https://www.salesforce.com/blog/crmarena-pro/)
- Einstein Activity Capture 把数据放在 Salesforce 之外的 AWS，有保留期限（6/24 个月）——说明“第三方捕获服务”会带来数据位置和保留期问题 — [Revenue Grid](https://revenuegrid.com/blog/einstein-activity-capture-limitations/)
- 第三方 WhatsApp CRM 浏览器插件存在被扫描为可能恶意的案例 — [ExtensionAuditor](https://extensionauditor.com/scan/wa-ojcllaigkeajeghbikbdanpoegecfikj)
- 企业微信会话存档要求企业自己部署解密（官方 SDK 仅 Linux/Windows C/Java） — [gitee wework-msgaudit](https://gitee.com/worksmart/wework-msgaudit)

### Inferences
- 成本推算（非来源数据）：假设 4 人每天共处理 200 条消息，每条整理/抽取约 2000 输入 + 300 输出 token，用 Haiku 4.5 级别模型：每天约 0.4M 输入 + 0.06M 输出 ≈ $0.7/天，约 $20/月；用 Flash 级别约 $6/月。客户速览、草稿按需触发，增量不大。结论：LLM 费用不是决策瓶颈。
- 延迟：整理/抽取放后台异步做（消息进来后排队处理），核心页面不等待 AI——这与 AGENTS.md “核心页面不能等待 AI” 的要求一致；草稿和速览是用户主动点的，几秒延迟可接受。
- 隐私：客户对话和报价是敏感商业数据。建议选择承诺“不用 API 数据训练”的商业 API；只发送需要的那段对话，不发整库；在 Trosa 自己的 ECS 上做捕获和存储，避免把原始对话长期放在第三方插件服务商处；AI 只给建议，写入走现有审计与撤销。
- 数据安全要求（用户全局规则：客户数据与沟通记录要自动、多地点、每周备份）同样适用于新捕获的 WhatsApp/邮件原文和附件，捕获量上来后备份体积会显著增加。

### Gaps
- Anthropic、Google、OpenAI 当前官方价格页本次未直接打开核对；各家 API 的数据保留/不训练承诺条款未逐一核对。
- 没找到关于中国大陆访问海外 LLM API、或数据出境（客户含中国供应商信息）合规要求的资料。
- 对 WhatsApp 语音、图片（报价单截图、样品照片）做多模态处理的成本未估算。
