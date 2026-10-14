# 524 重现：网页请求等慢模型超过 Cloudflare 上限

现象：`app.trosa.space` 显示 Cloudflare「A timeout occurred / Error code 524」。524 表示源站（ECS 上的 Waitress）在约 100 秒内没有回应。

原因：收件箱「AI 整理」（`/api/inbox/analyze-reply`）、识别截图（`/api/inbox/extract-image`）、智能导入（`/api/customers/smart-import`）和记录沟通时的 AI 摘要（`_summarize_raw_communication`）都在网页请求里同步调用模型或网站，单次超时分别是 180 秒、120 秒，网站读取链路最长可超过 100 秒，没有总时限。模型或网站一慢，请求就超过边缘的 100 秒上限。

## 重现（修复前）

修复前的代码导出到临时目录（不切分支、不用 stash）：

```bash
mkdir -p <临时目录>/before && git archive <修复前的提交> | tar -x -C <临时目录>/before
```

启动一个慢速模型桩（回答前等 110 秒，超过 Cloudflare 的 100 秒）和隔离开发服务，然后登录并计时一次 AI 整理：

```bash
.venv/bin/python tools/repro_slow_provider.py --port 18199 --delay 110
cd <临时目录>/before && LLM_BACKEND=deepseek DEEPSEEK_API_KEY=repro-key DEEPSEEK_BASE_URL=http://127.0.0.1:18199 \
  .venv/bin/python tools/repro_dev_server.py --app-root . --port 18190 --db-dir <临时目录>/db-before
curl -s -c jar.txt -b jar.txt -X POST http://127.0.0.1:18190/api/auth/login -H 'Content-Type: application/json' -d '{"user":"hamid"}'
curl -s -c jar.txt -b jar.txt -X POST http://127.0.0.1:18190/api/inbox/analyze-reply \
  -H 'Content-Type: application/json' -d '{"content":"Hi, we need 500 pcs acrylic signs, please send quote.","direction":"inbound"}' \
  -o body.json -w 'HTTP %{http_code}  total %{time_total}s\n'
```

结果：`HTTP 200  total 110.026889s`。已超过 Cloudflare 的 100 秒，边缘会先返回 524。

## 修复后验证

同样的命令，`--app-root` 指向修复后的工作树（`tools/repro_dev_server.py` 与 `tools/repro_slow_provider.py` 相同）。

结果：`HTTP 200  total 60.010542s`，返回 `ai_available: false`，即和未配置 AI 时相同的回退（摘要取原文）。60 秒的总时限落在 Cloudflare 的 100 秒之内，留出了数据库和隧道的余量。

## 不能证明的部分

- 未读取 ECS 上 2026-10-14 13:28（北京时间）前后的正式日志，所以不能确认那次 524 具体是哪一个请求。这里证明的是：任何一个慢模型或慢网站都会让上述请求超过 100 秒。
- 这是服务端耗时缺陷，不是界面渲染缺陷，所以没有用内置浏览器截图；界面上用户看到的是已有的「AI 不可用、保留原文」回退。
- 单元测试见 `tests/test_outbound_budget.py`：修复前 9 项全部失败（其中两项直接看到 180 秒与 100 秒的超时），修复后全部通过。
