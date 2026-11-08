# 客户页「该联系」一行：真实浏览器验收

- 脚本：`tools/repro_customer_followup_hint.cjs`（合成客户数据；用 `tools/repro_seed_followups.py` 写入建议，隔离开发库）。
- 修复前（上一版：今天页区块 + 三个按钮，客户页没有这一行）：退出码 20，`before/`。修复后：19/19 项通过，退出码 0，`after/`。
- 验的是：今天页不多任何东西；客户页「下一步」下面有一行带日期的理由、没有按钮；记了新沟通或安排了带日期的下一步，这一行自己消失；换客户不残留；iPad / iPhone / 旧 Win10 / 200% 缩放不横向滚动、不裁切。
- 截图：`after/02-customer-hint-1440.png`（客户页）、`03-customer-cleared-1440.png`（记沟通后）、`04-inbox-morning-note-1440.png`（早上收件箱那句话）、`0-*.png`（各视口）。
- 运行：`node tools/repro_customer_followup_hint.cjs --url <服务地址> --app-root . --db-dir <该服务的临时库目录> --label after --out docs/repro/customer-followup-hint/after`
