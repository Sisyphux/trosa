# Sela 发送屏障接口（本地待合并）

此接口使用 Sela 服务凭据；Trosa 不接收收件人原始地址或邮件正文。Sela 必须自行根据实际完整收件人集合生成规范的小写 64 位 SHA-256 十六进制 `recipient_hash`，并提取所有收件人域名。摘要的输入规范由 Sela 客户端约定；Trosa 只能比较摘要，无法验证摘要和域名是否覆盖实际收件人。

- `POST /api/integrations/sela/send-barrier/prepare`：传 `send_id`（UUID）、`recipient_hash`，以及 `name`、`domain`、`email_domain` 或 `domains` 中至少一项身份信息。`domains` 可省略（等同空列表），提供时必须是最多 12 个非空域名字符串的列表；不传邮箱地址或正文。服务核验数据覆盖后返回 token，有效期五分钟。
- `POST /api/integrations/sela/send-barrier/arm`：传相同 `send_id`、token、`recipient_hash` 和相同身份字段。`domains` 的内容、顺序和字符串必须与 prepare 一致；改动或省略非空列表会被拒绝。摘要无效或缺失返回 400；摘要或身份与准备时不同返回 409。即使屏障已处于 ARMED，重复请求也必须携带匹配摘要和身份。
- arm 会在持锁状态下检查所有给定域名和名称。匹配到其他负责人或归属不确定的账户时返回 409，不进入 ARMED；查询不完整或发生服务错误时也不会放行。调用方必须仅在 `acquired=true` 时发信，`acquired=false` 表示已有发送尝试持有屏障，不可再次发信。
- `POST /api/integrations/sela/send-barrier/finalize`：传 `send_id`、token 和 `outcome`（`SENT`、`CONFIRMED_NOT_SENT` 或 `UNKNOWN`）。UNKNOWN 不释放屏障；只有确认发送或确认未发送的终态释放。结果不明确时不能假定未发送。

屏障持有期间组织内相关公司身份和归属更改会被阻止；与已有账户无关的域名不会仅因没有匹配而被拒绝。此契约依赖调用方提供完整、真实的域名集合和摘要，无法防止调用方遗漏域名或对实际收件人计算错误摘要。
