# 房间：Inbox

- 来源原型：`../visual-exploration/rooms-v5/inbox/01-lightbox.html`（2026-09-30 选定；选定人：用户）
- 状态：已选定（尚未实现）

## 隐喻
摄影师在灯箱上看底片：光只打在正在看的那一张上，其余退到暗处。判断一条信号 = 读懂来源原文 + 校对 AI 的整理，两件事并排摆在同一块亮面板上。

## 动词
判断信号（不是通知流）：
- 逐张放大一条信号，读原文与 AI 整理（1 步：`J` / `K` 或点底片带）。
- 选四个处理方向之一：记录最新进展 / 安排下一步 / 暂不安排下一步 / 查看分析（选中后 1 步，`1 2 3`）。
- 撤销最近一条（常驻，`Z` 或 `⌘/Ctrl+Z`）。

## 空间模型
- 昏暗的房间：上半是一块亮着的灯箱面板（左：来源原文；右：AI 建议 + 处理），下半是一条底片带（按到达顺序，当前一张永远居中）；面板与底片之间有一道向下收窄的投影，金色刻度标出当前位置。
- 右下角收片盒放最近处理的两条，随时撤销。
- 桌面为主；手机上面板变成一列长页面（AI 与动作在前、原文在后），底片带退到页尾，缩略信息只留「公司 + 时间」。

## 招牌交互
逐张放大、处理后被收走：`J` / `K` 或点底片带移动时，当前底片向上抽离、底片带自动合拢、下一张被光投到面板上；处理（`1 2 3`）后被收进收片盒，撤销可放回原位。键盘等价：`J` / `K` 前后移动 · `1 2 3` 处理 · `Enter` 主要动作 · `A` 查看分析 · `Z` 撤销 · `Esc` 收回。表单内 `Enter` 提交（多行用 `⌘/Ctrl+Enter`）；原因单选方向键切换；候选客户 `Enter` 先选后确认。触屏等价：点底片带 / 面板（未实测）。减少动态下动画瞬时到终态。

## 意外细节
承诺高亮：来源原文中的数量、价格、日期、交期（3,000 pcs、€2.40、Friday、11 月底…）被自动点亮，其余文字保持常规，`H` 可关闭。价格与交期承诺永远由人完成，而这些恰恰是判断时最该盯的词，省掉在长邮件里逐字找数字。

## 数据来源 / 接口
- 信号列表：`GET /api/inbox` → `questions[]`：`kind`、`kind_label`、`headline`、`why`、`known_facts`、`evidence[]`（`source_label`、`date`、`detail`、`identity`）、`suggested_customer`、`customer`、`options[]`、`source_type`、`created_at`。
- AI 整理：`POST /api/inbox/analyze-reply` → `analysis`：`summary`、`intent`、`key_facts`、`needs`、`direction`、`message_date`、`suggested_next_action`（无日期、无置信度、无引用原文位置）；候选 `candidates[]`（含 `score`）。AI 内容一律标注「AI 建议」。
- 记录最新进展：沿用 `record_customer_communication`（`recordInboxReply` / `openCommunicationConfirm` 同一事务），返回 `undo_token`；撤销用 `POST /api/inbox/undo-record-reply`。
- 安排下一步：`POST /api/customers/{id}/tasks`（`title` + `due_date` 必填）；撤销用 `DELETE /api/reminders/{id}`。
- 关闭信号 / 同一主体 / 不同主体 / 批准 / 跳过：`POST /api/inbox/{id}/decide`；Sela 请求回答：`POST /api/inbox/questions/{id}/respond`；日期密度参考 `GET /api/reminders/upcoming`。
- 承诺高亮为纯前端对 `evidence[].detail` 做正则，不依赖后端。
- 完整的字段与接口映射见来源原型 README（本任务不带入 worktree）。

## 落地前要补的缺口
- **`decide` 的撤销令牌（后端 / 或前端延迟提交）**：原型的撤销在前端模拟。真实实现需给 `decide` 补 `undo_token`，或做「延迟提交」（前端持有 8–12 秒到期再调用）。
- **「确认归属」合并进「记录」（已定）**：不新增独立端点。确认归属与记录本就是同一事务（`_assign_inbox_customer` 只在 `decide(same)` 里被调用），原型把它拆成两步；实现时合并为「确认归属并记录」一个动作，避免新的写入路径。
- **`suggested_customer` 缺少 `score`（后端 / 或前端隐藏）**：`/api/inbox` 的 `suggested_customer` 目前只有 `customer_id` / `company` / `reason`，原型的 74% / 62% / 41% 需要后端补 `score`，或改成不显示。
- **「暂不安排下一步」复用现有关闭信号（已定）**：不补新端点、不引入新的写入路径，先复用现有 `decide` 的关闭信号语义（关注状态仍为 `waiting_reply` / `no_near_term_need` / `monitoring`）。
- 以上凡涉及写入的，遵守「不新增写入语义」：优先复用现有事务，不新增独立业务写操作。

## 状态
- 加载：骨架，`role=status` 的「正在加载」。
- 失败且无内容：不显示成「没有数据」，给出重试。
- 刷新失败（stale）：保留已加载内容 + 横幅 + 重试。
- 已清空（empty）：底片带清空；撤销入口仍然在（演示状态自带可撤销记录）。
- 未登录 / 权限 / 解析错误：原型未画；实现沿用现有页面的区分。
- 处理前显示会发生 / 不会发生什么（`completion_effects` / `will_not_do`）。

## 禁忌
- 默认不做：自动创建客户、联系人或待办。理由：AI 只给候选与建议，不替人选择，也不产生商业承诺。
- 默认不做：歧义信号在「确认归属」前被记录或安排下一步。
- 默认不做：出现第二个实心元素。理由：整页唯一实心是当前信号最主要的处理动作（打开表单后移到提交按钮）。
- 默认不做：把失败画成「没有数据」。
- 未选其它方向（一两句）：02 验货台（去掉轨道与出口后接近通用三窗格分诊界面）与 03 信件格（4×4 格柜随客户数增长会失控）。用户选定 01 灯箱——读原文最舒服、「一屏一件」最接近「光即注意力」；落选方向的细节见其原型 README，本文不复制。
- 突破世界默认处：无（柔光为背景光，不承载文字或数据）。

## 验收
- 缩略图测试：与其它房间并排时应可辨为「一块大亮面板 + 底部一条底片带」。
- 去 logo 自检：亮面板 + 底片带 + 投影没有通用对应物，不应像通用 SaaS。
- 真实浏览器矩阵（**均未验证**）：宽屏 1440×900 / 1024×768 / 390×844、iPad 真机触控、iPhone 真机、Safari / Firefox、旧 Win10、键盘全流程、200% 缩放、系统级减少动态、性能模式。
- 数据状态：19 条信号（含同客户多条、带附件、自动回复、新联系人）、待归属 / 歧义、Sela 两类、空白、加载中、失败、刷新失败、已清空。
- 焦点环：房间内统一 `2px solid var(--dl-focus)`，外移 3px；`--dl-focus` 即 `--dl-gold-ink`，纸面对比度 ≥3:1（`tools/check_daylight_contrast.py` 通过）。亮金 `#bc9052`（2.85:1）只作装饰，不作焦点环。
- 回归：客户、联系人、沟通记录、Today、Inbox、Search、备份恢复、Sela 幂等同步未受影响。
- 性能：底片带 `translateX` 与面板柔光（大面积 radial-gradient + blur）需在性能模式关闭柔光后复测低端设备。
