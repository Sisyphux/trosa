# 外贸"成交单据"这一侧：报价、样品、PI、订单、出运跟踪、收款——工具做法与 Trosa 该建多轻

说明：调研时间 2026-10。来源中厂商自写内容（OKKI 官网、Zoho 中文营销文章、Alibaba 卖家博客、Terminal49 自家对比页）占比高，已在条目里标注"厂商说法"。孚盟、Freightos/Flexport、PandaDoc、国内"外贸单证软件"没有搜到可靠的一手资料，列在各节 Gaps。

---

## 1. 主流工具怎么处理报价、价格表、PI、订单状态、出运跟踪、收款提醒

### Takeaway
外贸专用 CRM（OKKI/小满）已经把"报价单 → PI → 销售订单 → 回款认领/核销"做成一条链，最近一年的方向是"AI 从聊天和邮件里自动认出 PI/报价/付款进度并挂到商机上，人只确认"；通用 CRM（HubSpot、Pipedrive）只做到"报价/文档生成"，订单和收款交给财务软件（Zoho Books、Odoo）。对 4 人团队来说，最值得借鉴的是"自动识别+人确认"，而不是 OKKI 那套审批、智能锁、毛利核算。

### Cited Findings
**OKKI / 小满（外贸专用 CRM）**
- 官方称可结合沟通记录、商机、订单数据和 AI 知识库生成报价草稿，交业务员确认，之后跟进报价与回款 — [OKKI CRM 产品页](https://www.xiaoman.cn/products/crm.html)
- 更新日志（厂商说法）："再生成可审核的询价、报价文件或PI 草稿，最终交给业务员确认" — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- AI 把"聊天和邮件中的PI、CI、规格书等附件"自动关联到对应商机文档；还能识别"运输方式、付款方式和进度、生产进度等关键节点，并同步至商机资料详情" — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- 回款："业务员认领回款、核销订单更便捷"，支持批量登记、超额回款、查看汇兑损益；回款（含手续费）与订单关联 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- 销售订单：可同时在阿里国际站起草信保订单；订单毛利可扣除佣金、手续费；"智能锁"在指定阶段锁定核心字段，解锁可走审批；报价/订单审批推送到钉钉 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- 报价侧：可设置更精细小数位、报价公式；询价/报价/订单/采购单支持自定义字段（长宽高体积可计算）；多个采购员可对同一询价产品反馈不同供应商报价供比价 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- 样品只以"里程碑提醒"形式出现：管理者可收到"如确认报价、样品送达等里程碑节点"提醒 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- 第三方介绍称商机阶段可从询盘流转到报价、打样、订单、复购，并绑定报价单、PI、合同、物流、收款记录；同时评价"价格偏高、功能较复杂，新手上手约需 1–3 天" — [一路火系统介绍文](https://ruanwen.ylhai.cn/article/detail-51783815381xu81me-1.html)（二手营销文，可信度一般）
- 第三方评测称报价/PI 与订单跟单打通，与阿里国际站集成是优势 — [itppt 小满评测](https://www.itppt.com/sites/xiaoman-crm/)

**HubSpot Quotes**
- 只在单个报价里临时加的行项目不会进入产品库，产品库改价不会同步到已有报价 — [Insycle 关于 HubSpot 行项目](https://blog.insycle.com/managing-hubspot-line-items)
- 每个 deal 的行项目上限 2023 年从 100 提到 200，仍有用户抱怨不够 — [HubSpot 社区帖](https://community.hubspot.com/t/deals-with-greater-than-100-line-items/76890)
- 用户抱怨（2024，非官方）：报表最多 20 个属性、行项目没有列表报表、净价计算不灵活、行项目无校验规则 — [HubSpot 社区帖](https://community.hubspot.com/t5/APIs-Integrations/Deals-with-greater-than-100-line-items/td-p/766798)
- 报价模板的自定义模块不能通过 quotes API 加入；默认行项目模块可隐藏但必须保留 — [HubSpot 开发文档](https://developers.hubspot.com/docs/cms/start-building/building-blocks/modules/quotes/overview?facet3=pdf)

**Pipedrive Smart Docs**
- 模板可从产品目录、deal/联系人字段取数，生成报价表格；可看到对方打开文档，有电子签名；Professional 及以上套餐含，低套餐需加购 — [Pipedrive Smart Docs](https://blog.pipedrive.com/features/smart-docs)、[Business Wire 2021](https://www.businesswire.com/news/home/20210421005493/en/Pipedrive-Introduces-Smart-Docs-to-Enhance-Remote-Sales-and-Help-Close-Deals-Faster/)
- 没找到官方"形式发票（PI）"功能；第三方插件 ExtraflowDoc 补报价/发票 — [G2 ExtraflowDoc](https://www.g2.com/products/extraflowdoc/discuss)

**Zoho Books（财务侧）**
- Retainer invoice（预收款发票）用于收预付款，在交付前开具，收到的钱记为负债而不是收入；可部分/全部冲抵后续发票，可在客户接受报价时收取 — [Zoho Books Retainer 帮助](https://www.zoho.com/za/books/help/retainer-invoice/functions.html)
- 有用户在社区问"报价上如何同时显示总额、定金和尾款"，说明定金/尾款在报价单上的表达不是开箱即用 — [Zoho 社区帖](https://help.zoho.com/portal/ja/community/topic/deposit-on-invoice-10-8-2025)
- Zoho 中文文章描述的链路：询价 → 报价单 → PI → 发货安排 → 收款确认（营销文） — [Zoho 中文：外贸单证](https://www.zoho.com.cn/books/articles/tag/外贸单证)

**Odoo**
- 需在销售设置里开启"形式发票"，开启后任何报价/销售订单都能发 PI，收件人自动带出客户 — [Cybrosys Odoo 16 PI 指南](https://cybrosys.com/blog/how-to-send-pro-forma-invoice-to-your-customers-in-odoo-16-sales)
- 原生每张销售单只有一个币种；要在一张报价里多币种显示需要第三方模块（Viindoo），该模块还加了汇率条款、未录入汇率不允许过账外币发票 — [Viindoo 多币种报价模块](https://viindoo.com/apps/modules/19.0/viin_sale_multi_currency)

**Quotient（轻量报价工具）**
- G2 小企业用户：模板报价可快速发出，"报价追踪"帮助跟进；缺点是模板设计选项有限、不适合大型投标 — [G2 Quotient 评价](https://www.g2.com/products/quotient/reviews)
- Xero 应用市场 145 条评价均分 4.9；好评点是图片、预设条目、可选项让客户自己勾选 — [Xero App Store Quotient](https://apps.xero.com/au/app/quotient/reviews)
- Capterra 有评价称设置难、定制费时 — [Capterra 对比页](https://www.capterra.com/compare/131897-153794/Quotient-vs-Better-Proposals)

### Inferences
- 两类路线：①外贸 CRM（OKKI）把单据都塞进 CRM，越做越像 ERP（审批、锁字段、毛利、核销）；②通用 CRM 只管"报价文件"，订单/收款交财务软件。Trosa 的 4 人团队两头都不需要完整版，合理的是"报价记录 + 订单进度节点 + 收款节点"挂在客户时间线上。
- OKKI 2025–2026 最有价值的方向与 Trosa 现有设计（Inbox 捕获 → 整理 → 人工确认）高度吻合：从邮件/WhatsApp 识别 PI、报价、付款、生产进度，生成候选给人确认。这比让业务员手填表单更可能被坚持使用。
- 样品在主流工具里几乎都不是独立对象，只是商机的一个阶段/里程碑。

### Gaps
- 孚盟（Fumamx）的报价/样品/出运功能没有搜到一手资料。
- PandaDoc、Freightos、Flexport 本轮未取得资料。
- 国内"外贸单证软件"（如单证通类）没有找到可靠来源。
- OKKI 各套餐（基础/Pro）功能边界和价格未确认。

---

## 2. 最小可用"报价记录"：业务员真正会维护什么、以后要查什么

### Takeaway
业务员最常见的回头查询是"上次给这个客户报的什么价"，Excel/邮件管理下常常找不到。最小报价记录 = 客户 + 日期 + 产品/规格 + 单价 + 币种 + 贸易术语 + 有效期（+ 可选 MOQ、当时汇率、状态）。价格表分层、成本拆解、自定义价格这些是 ERP 层面的东西，小团队可以不做。

### Cited Findings
- Zoho 中文文章（营销文）：客户说"上次报的是 50 美元"，业务员翻遍邮箱也找不到当时的报价单；Excel 报价容易版本混乱、数字出错 — [Zoho 中文：专业报价单](https://www.zoho.com.cn/books/articles/tag/专业报价单)
- 同类文章指出汇率影响：报价时 7.05，下单时 6.95，收款时 6.88，因此历史报价应记录日期与所用汇率 — [Zoho 中文：外贸报价管理](https://www.zoho.com.cn/books/articles/tag/%E5%A4%96%E8%B4%B8%E6%8A%A5%E4%BB%B7%E7%AE%A1%E7%90%86)
- 同一来源建议：报价单应注明产品规格、包装方式、交货期、付款条件 — [Zoho 中文：外贸单证](https://www.zoho.com.cn/books/articles/tag/外贸单证)
- 价格分层文章建议的字段：产品（SKU、描述、单价、图片）；客户（语言、结算币种、价格组、自定义价）；报价（有效期、付款条件、FOB/CIF、锁定汇率）；成本（采购价、供应商、运费、关税、保险、目标利润）。并建议报价生成时锁定汇率、有效期到期提醒、保存报价状态（已看/接受/拒绝）、汇率波动超过阈值（例"超过3%"）重新议价 — [Zoho 中文：分层定价](https://www.zoho.com.cn/books/articles/jiage0603.html)
- 对小客户建议并列给 FOB vs CIF 两个价、让价附带条件（例"接受90天L/C可再降3%"） — [Zoho 中文：分层定价](https://www.zoho.com.cn/books/articles/jiage0603.html)
- OKKI 增加了"更精细小数位"、报价公式、长宽高体积可计算的自定义字段——说明板材/尺寸类产品的报价单位与换算是真实痛点 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- HubSpot 报价里临时加的产品不进产品库——意味着"按产品查历史价"在通用 CRM 里也不好做 — [Insycle](https://blog.insycle.com/managing-hubspot-line-items)

### Inferences
- 针对亚克力板/膜，建议 Trosa 的报价行最少字段：产品类别（板/膜）、厚度、颜色、尺寸（可选）、单价、计价单位（张/kg/m²）、币种、贸易术语（FOB/CIF/EXW 等）、有效期、MOQ（可选）、报价日期。用户自己的业务规则"上颜色另加 0.15 USD"可作为输入时的提示，而非复杂价格表。
- "报价"不必是独立大表单：可以是沟通记录的一种附属结构（一次报价 = 一条带价格行的沟通记录），这样"上次报价"就是在时间线里按产品/厚度筛选。这与 Trosa"记录实际事实"的理念一致，也避免新增一个需要维护状态的大对象。
- 最省力的录入方式：从已发出的邮件/WhatsApp 报价或 PDF 中由 AI 抽取价格行，业务员只确认（OKKI 已在做同样的事）。
- 不建议做：价格组/客户分层价格表、成本拆解、毛利计算、审批——这些是 4 人团队在 Excel 里也没在做的事，做进 CRM 只会变成负担。

### Gaps
- 没有找到独立（非厂商）的调研数据说明业务员实际维护哪些报价字段；知乎/福步原帖本轮未检索到。
- 没有找到"报价→成单转化率"的行业统计。

---

## 3. 样品跟踪做法及其对转化的作用

### Takeaway
样品是外贸成交的关键关口，但行业做法很朴素：寄出后第一时间把运单号、预计到达告诉客户，确认收到，再追测试反馈。工具里样品通常只是一个"里程碑"，最常见的失败是"寄出后没人跟反馈"。

### Cited Findings
- 寄出后"第一时间通知客户，包括样品的详细信息、运单号码、寄送时间和预计到达时间"，请客户确认收到并跟进测试反馈；可建样品管理表记录国别、客户、批次 — [AMZ123 问答](https://www.amz123.com/ask/C7iFSCPz)
- 样品费：低价值样品不宜单独收费（汇款手续费可能比样品费高），可与运费合并收；高价值样品可先收，客户下单达一定金额后退还 — [AMZ123 问答](https://www.amz123.com/ask/C7iFSCPz)
- 新客户一般让客户承担快递费，用客户自己的进口快递账号（courier account）；到付多用于费用高或新客户、成交不确定的情况 — [AMZ123 问答](https://www.amz123.com/ask/0WVSCfWr)
- 样品寄出后没有及时确认反馈是常见"跟丢"情形；客户可能"一个月后才确认样品"，靠记忆跟进不可靠（厂商营销文） — [Zoho 中文：询盘跟丢](https://www.zoho.com.cn/crm/articles/trade260520.html)
- DHL：寄样前先明确用途、属性、时效，注意文件合规和报关申报准确性 — [DHL B2B 寄样指南](https://www.dhl.com/discover/zh-cn/logistics-advice/b2b-sample-shipping-guide)
- OKKI 把"样品送达"作为里程碑提醒，而不是独立模块 — [OKKI 更新日志](https://www.okki.com/upgrade.html)

### Inferences
- Trosa 不需要"样品对象"，用现有能力即可覆盖：一条"寄样"沟通记录（寄了什么规格、快递公司、单号、谁付运费）+ 一个带日期的待办"追样品反馈"。可以在记录"寄样"时默认建议一个 7–14 天后的跟进待办（需人工确认，符合"待办必须有动作和日期"）。
- 快递单号可以直接生成 DHL/FedEx/UPS 跟踪链接，不必接 API。
- 样品转化率统计（寄样→反馈→下单）可以从记录里事后算，不需要业务员额外维护状态。

### Gaps
- 没有找到可靠的"寄样后成交率"行业数据。
- 快递（DHL/FedEx）跟踪 API 对小团队的价格与接入门槛本轮未调研。

---

## 4. 订单生命周期：哪些节点重要，能否自动跟踪

### Takeaway
外贸订单常见节点是：收定金 → 生产 → 订舱 → 装柜 → 开船(ETD) → 到港(ETA) → 收尾款 → 放单。对业务员和客户最关键的是"定金到了吗、什么时候开船、什么时候到、尾款收了没、提单放了没"。海运段（开船、到港、ETA 变化）可以用集装箱跟踪 API 按提单号/柜号自动拿到，价格已经低到小团队可接受；生产和订舱前的节点只能靠人或从聊天里识别。

### Cited Findings
- **ShipsGo**：预付信用点，无月费，注册送 3 点；示例 500 点 1000 美元（约 2 美元/点）；按提单号跟踪只扣 1 点，不论提单下有 1 个还是 10 个柜；邮件通知不限 — [ShipsGo 定价](https://shipsgo.com/pricing)、[ShipsGo FAQ](https://shipsgo.com/de/faq)
- ShipsGo 信用点有效期：Capterra 写 1 年，ShipsGo 自己写不过期——来源冲突 — [Capterra ShipsGo](https://www.capterra.co.nz/software/1042364/shipsgo)；[ShipsGo 定价](https://shipsgo.com/pricing)
- ShipsGo API 似乎另收年费，需要找销售（第三方说法，官方只写"联系我们"） — [RFP.wiki ShipsGo vs Terminal49](https://www.rfp.wiki/supply-chain-logistics-transportation/transportation-logistics/real-time-transportation-visibility-platforms/logistics-data-platforms/shipsgo/terminal49)
- **Terminal49**：免费层每月 50 票（含 ETA、码头数据、节点提醒）；按需付费每柜 10 美元，含 API 和 webhook；节点包括提空箱、重箱进港、装船、卸船、到港、提重箱、还空箱；关键节点最快每小时刷新；直连 36 家船公司、100+ 码头，码头扣货/费用/LFD 数据集中在美国加拿大 — [Terminal49 定价](https://terminal49.com/pricing)、[Terminal49 API](https://terminal49.com/container-tracking-api)、[Terminal49 覆盖](https://terminal49.com/docs/coverage/home)
- Terminal49 免费层另一处写"开发者 Key 最多 10 个活跃柜"——数字不一致 — [Terminal49 vs Vizion](https://terminal49.com/terminal49-vs-vizion)
- Terminal49 自家对比页称 Vizion 覆盖船公司"多得多"；Vizion 价格未公开 — [Terminal49 vs Vizion](https://terminal49.com/terminal49-vs-vizion)
- **SeaRates**：按柜计费，API、增加用户、接入协助不另收费；起价约 25 美元/月（最多 10 票）；访客每天免费 1 票、注册用户每月 5 票 — [SeaRates 跟踪 API](https://www.searates.com/integrations/api-container-tracking/)、[SeaRates 帮助](https://www.searates.com/help/tools/tracking-system)
- OKKI 用 AI 从聊天中识别"运输方式、付款方式和进度、生产进度"并同步到商机——说明出运前节点主流做法也是"从沟通里抽取"而非对接系统 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- 2026 年行业文章讨论集装箱跟踪的计价模式与隐藏成本（按柜 vs 订阅 vs API 另收费） — [Container News 2026](https://container-news.com/the-real-cost-of-container-tracking-pricing-models-hidden-expenses-and-how-to-choose-the-right-platform-in-2026/)

### Inferences
- Trosa 的"订单"可以极简：一张订单卡 = 客户 + PI 号/金额/币种 + 付款条款 + 一串日期节点（定金到账、预计完工、订舱、开船 ETD、到港 ETA、尾款到账、放单）。每个节点只是"日期 + 是否完成"，挂在客户工作区"现在/下一步"里。
- 海运段可选自动化：业务员只填提单号或柜号，后台定期查 ETA/开船/到港，变化时进 Inbox 提醒（不自动改待办）。按 Trosa 每月出柜量（推测几十柜以内），ShipsGo 按提单 1 点约 2 美元、Terminal49 免费 50 票/月或 SeaRates 25 美元/月都够用；但注意 Terminal49 的码头细节只覆盖美加，中东/印度/拉美客户主要只能拿到船公司层的 ETA。
- 第一版可以不接 API：只存提单号并生成船公司官网跟踪链接，等确实频繁查 ETA 再接。
- "到港前 X 天提醒收尾款/放单"是最有价值的自动提醒，它把出运节点和收款节点连起来。

### Gaps
- 没有找到"客户最关心哪个节点"的调研数据；以上重要性排序是推断。
- 印尼港口（雅加达/泗水）发出航线在各 API 中的覆盖质量未验证。
- Flexport/Freightos 的跟踪能力未调研。

---

## 5. 收款与信用风险：定金/尾款提醒、逾期

### Takeaway
小外贸团队最常见的是 T/T 30% 定金 / 70% 尾款，尾款"见提单副本付"比"出货前付"风险高；新客户首单通常要求出货前付清尾款，老客户才放宽。工具层面，OKKI 做回款认领与核销，Zoho Books 用预收款发票处理定金；对 Trosa 来说，关键只是"每笔订单的应收、已收、下次该收的日期"。

### Cited Findings
- Alibaba 卖家博客（实务观点，非权威）：30% 定金 + 70% 见提单副本付，对卖方风险"中高"；30/70 出货前付清为"低中" — [Alibaba 卖家博客：30% 定金](https://seller.alibaba.com/blogs/2026/southeast-asia/apparel-accessories/30-deposit-payment-terms-guide-alibaba-b2b)、[Alibaba 卖家博客：30/70](https://seller.alibaba.com/blogs/2026/southeast-asia/dried-fruit/30-70-payment-terms-guide-alibaba-b2b)
- 同类来源：见提单副本付款适合信任的老客户/返单，首单一般要求出货前付 70% — [Alibaba 卖家博客：T/T](https://seller.alibaba.com/blogs/2026/southeast-asia/dried-fruits/tt-payment-terms-procurement-guide-alibaba-b2b)
- 大额或高风险订单可用信用证（L/C）降低卖方风险 — [Alibaba 卖家博客：T/T 交期](https://seller.alibaba.com/blogs/2026/southeast-asia/baby-pants-shorts/delivery-tt-payment-terms-guide-alibaba-b2b)
- 买方视角：英国采购指南建议买方首单坚持 30/70 或用信保，避免 100% 预付 — [Epic Sourcing](https://www.epicsourcing.co.uk/epic-guides/how-to-pay-chinese-suppliers-tt-lc-trade-assurance)
- OKKI 回款：认领、核销、批量登记、超额回款、汇兑损益、回款含手续费 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- Zoho Books：定金用预收款发票，记为负债，后续冲抵正式发票 — [Zoho Books Retainer](https://www.zoho.com/za/books/help/retainer-invoice/functions.html)
- 汇率随时间变动（报价 7.05 → 下单 6.95 → 收款 6.88），系统应能显示收款时的汇兑差 — [Zoho 中文：外贸报价管理](https://www.zoho.com.cn/books/articles/tag/%E5%A4%96%E8%B4%B8%E6%8A%A5%E4%BB%B7%E7%AE%A1%E7%90%86)

### Inferences
- Trosa 应做的只是"收款节点"：订单上的定金、尾款各一行（应收金额、币种、约定条件如"出货前/见提单副本"、实际到账日期与金额）。尾款条件为"见提单副本"的订单，在开船后自动建议"催尾款"待办；逾期未到账在 Today 里显眼提示。
- 不做：会计记账、核销、汇兑损益、银行流水对接、信用额度——这些属于财务软件，且四人团队用不上。
- 银行到账通知（邮件）可以作为 Inbox 捕获来源，AI 识别"收到 xx USD"后建议把某笔收款标为到账，人确认。

### Gaps
- 没有找到中立的中小出口商坏账/逾期统计；中信保等出口信用保险对小团队的实际门槛本轮未调研。
- 专门的催款工具（如 Chaser 等）本轮未检索。

---

## 6. 从结构化数据生成报价单 PDF / PI：最佳做法与坑

### Takeaway
生成报价单/PI 是所有工具的标配，常见坑是：模板设计受限、行项目与产品库不同步、多币种/定金尾款表达不顺、汇率不锁定。对 Trosa，最简可行是"一套中英文模板 + 从报价行/订单字段填充 + 生成后存为客户附件"，而非可视化模板编辑器。

### Cited Findings
- Pipedrive Smart Docs：模板从产品目录、deal/联系人字段自动取数，减少手抄出错；可追踪对方是否打开 — [Pipedrive Smart Docs](https://blog.pipedrive.com/features/smart-docs)
- Quotient 用户抱怨模板设计选项有限 — [G2 Quotient](https://www.g2.com/products/quotient/reviews)
- HubSpot：报价模板自定义模块不能用 API 加；默认行项目模块必须保留——模板灵活度受限 — [HubSpot 开发文档](https://developers.hubspot.com/docs/cms/start-building/building-blocks/modules/quotes/overview?facet3=pdf)
- Odoo 原生每张单一个币种，多币种显示要装第三方模块；该模块加入"未录入汇率不允许过账外币发票"的保护 — [Viindoo 多币种模块](https://viindoo.com/apps/modules/19.0/viin_sale_multi_currency)
- Zoho 用户想在报价上同时显示总额/定金/尾款，需要在社区求助 — [Zoho 社区帖](https://help.zoho.com/portal/ja/community/topic/deposit-on-invoice-10-8-2025)
- 建议报价生成时锁定汇率；按客户偏好语言与结算币种出单 — [Zoho 中文：分层定价](https://www.zoho.com.cn/books/articles/jiage0603.html)
- PI 不只是付款依据，也是进口商申请信用证、外汇额度的凭证，所以内容必须准确 — [Zoho 中文：形式发票 PI](https://www.zoho.com.cn/books/articles/tag/形式发票pi)
- OKKI 走的是"AI 先出 PI 草稿 → 业务员确认"的路线 — [OKKI 更新日志](https://www.okki.com/upgrade.html)

### Inferences
- PI 号、金额、币种、贸易术语、付款条款一旦发出应"冻结"（生成后存档 PDF，作为附件挂在客户时间线），修改就生成新版本，避免"系统里的数和客户手里的 PDF 不一致"。
- 用户的业务规则（"100% 新料"、不提供应商、颜色加价）应固化在模板文本和默认值里，减少每次手改。
- 多语言：外贸客户基本接受英文 PI；中东/拉美客户的本地语言模板非必需，可后置。
- 第一版完全可以不生成 PDF：很多团队已有 Excel 报价模板，Trosa 先"读取已发出的报价/PI 附件并抽取关键数据"，价值更大、改变习惯更少。

### Gaps
- 没有找到关于外贸 PI 生成"错误率/纠纷"的数据。
- 国内外贸单证软件的模板做法未调研到。

---

## 7. 什么时候变得太重（ERP 化）——失败模式证据

### Takeaway
ERP/重系统在中小企业失败，研究普遍指向"人"的问题（高层不投入、基层没人手、用户不配合），而不是技术本身；外贸 CRM 厂商自己也承认功能复杂、上手要几天。对 Trosa，判断线是：凡是需要业务员额外维护状态、审批、核算的字段，都是 ERP 化的信号。

### Cited Findings
- 美国中小企业 ERP 失败研究：主要原因是高层缺乏投入和领导支持，以及基层缺人手、缺少专门的变革管理 — [NSUWorks 论文](https://nsuworks.nova.edu/hcbe_facarticles/1059)
- 中国制造企业 ERP 上线后失败案例：不只是系统技术问题，更与高层、IT 人员、系统用户相关的关键问题有关 — [XJTLU 论文](https://scholar.xjtlu.edu.cn:443/en/publications/why-erp-post-implementation-fails-lessons-learned-from-a-failure-)
- 中国小企业 ERP 失败案例研究（提出 9 个关键成功因素，摘要未列具体内容） — [Emerald JMTM](https://www.emerald.com/insight/content/doi/10.1108/17410381111143130/full/html)
- OKKI 被第三方评价"价格偏高，功能较复杂，新手上手约需 1–3 天" — [一路火介绍文](https://ruanwen.ylhai.cn/article/detail-51783815381xu81me-1.html)
- OKKI 自身已在加入"智能锁 + 审批解锁"、订单毛利含佣金手续费、钉钉审批推送等管控功能 — [OKKI 更新日志](https://www.okki.com/upgrade.html)
- Capterra 上 Quotient 评价：设置难、定制费时；功能不能随业务增长定制 — [Capterra Quotient vs Better Proposals](https://www.capterra.com/compare/131897-153794/Quotient-vs-Better-Proposals)
- HubSpot 用户抱怨行项目上限、报表限制、无校验——通用 CRM 做订单/行项目时会碰到天花板 — [HubSpot 社区帖](https://community.hubspot.com/t5/APIs-Integrations/Deals-with-greater-than-100-line-items/td-p/766798)
- Zoho 中文文章提到小团队常见痛点：Excel 越来越乱、客户信息散在各业务员手里、月底对账繁重（营销文） — [Zoho 中文：高汇报](https://www.zoho.com.cn/creator/articles/gaohuibao.html)

### Inferences
ERP 化的具体信号（对照 Trosa 现有原则"别加状态/分类/指标"）：
1. 订单要维护多级状态枚举（草稿/待审/已审/生产中/…）而不是"日期节点是否发生"。
2. 报价/订单需要审批或锁字段。
3. 需要录入成本、算毛利、核销收款、算汇兑损益。
4. 需要维护产品主数据/SKU 库和分层价格表才能报价。
5. 出运单证（CI/PL/报关单/原产地证）在 CRM 里制作。
6. 库存、采购、供应商（中国供应商）进入 CRM——这还违反用户"不提供应商"的对外口径要求，应严格放在 Trosa 之外。

建议 Trosa 的边界：
- **做**：报价行（挂在沟通记录上，可按产品/厚度/颜色查"上次报价"）；寄样记录 + 跟进待办；轻量订单卡（PI 号、金额、付款条款、日期节点、提单号）；收款节点（定金/尾款应收与到账）；从邮件/WhatsApp/附件中 AI 抽取这些事实 → Inbox 人工确认。
- **可选后续**：提单号自动跟踪 ETA（ShipsGo/SeaRates/Terminal49 级别成本）；PI/报价 PDF 模板生成。
- **不做**：审批、毛利/成本、核销、汇兑损益、库存采购、产品主数据、单证制作、信用额度。

### Gaps
- 没有找到专门针对"外贸业务员不愿在 CRM 里录订单/报价"的调研或论坛统计；知乎/福步相关讨论本轮未检索到原帖。
- 没有找到 OKKI 订单/回款模块实际使用率的数据。
