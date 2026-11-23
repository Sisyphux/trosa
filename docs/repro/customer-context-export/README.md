# 导出全部沟通：剪贴板里是完整记录

## 结论

客户工作台顶栏的「导出全部沟通」以前只复制最近 50 条，邮件只给主题；历史记录里的「下一步」和当前待办混在一起，人工备注和自动背调也没有分开。现在同一颗按钮复制完整时间线：日期、渠道、方向、已存正文、结果都在，邮件区分我方正文与客户回信，当前未完成待办 / 人工备注 / 自动背调各自成块。

验证的是**真实剪贴板文本**，不是接口是否返回成功。脚本点击页面上的真实按钮，再读 `navigator.clipboard.readText()`，并与接口返回的完整文本逐字比对。

## 复现方法

```bash
# 1. 隔离服务（临时数据库，不要用仓库 data/）
.venv/bin/python tools/repro_seed_context_export.py --app-root . --db-dir /tmp/trosa-context-db
.venv/bin/python tools/repro_dev_server.py --app-root . --port 18190 --db-dir /tmp/trosa-context-db

# 2. 在另一个终端跑脚本
node tools/repro_customer_context_export.cjs --url http://127.0.0.1:18190 \
  --label after --out docs/repro/customer-context-export/after
```

修复前的对比：`git archive HEAD | tar -x -C <临时目录>` 得到干净副本，用同一份种子和脚本跑，结果写入 `before/`。

退出码：`0` 全部通过，`20` 至少有检查失败（缺陷存在），`21` 服务或浏览器不可用。

## 样本

种子数据是脱敏样本，不含真实公司、联系人、地址或价格表。它保留了一笔真实报价/图纸/寄样往来的形状：

- 客户按图纸询价；
- 我方已回复「原弯管方案做不了」；
- 客户原话「可以的话寄样」，导出后仍是原话，不会被写成「已寄样」；
- 60 条历史沟通（同一天多条）、三封开发信（有正文+回信 / 只有主题 / 退信）、一条未完成待办、人工备注和一份自动背调。

## 结果

| 运行 | 结果 |
| --- | --- |
| `before/`（修复前） | 退出码 20；8 项失败：第 50 条以后丢失、邮件无正文、回信没单列、只有主题的邮件没有说明、待办 / 备注 / 自动背调没有独立成块 |
| `after/`（修复后） | 退出码 0；12 项全部通过，剪贴板文本与接口返回一致（4422 字符） |

证据：各目录下的 `report.json`（每项检查、页面错误、字符数）和截图 `workspace-open.png`、`after-copy.png`。

## 修复点

- `app.py`：`export_customer_context` 在 timeline 模式下按已绑定的沟通 id 读取邮件的主题、正文、回信和退信状态；`_customer_timeline_markdown` 输出完整时间线、当前待办、人工备注和自动背调四块，不生成阶段、评分或建议。
- 排序按日期、保存时间、记录 id 稳定从新到旧；同一天多条不会因排序丢失或乱序。
- 其他导出模式（`compact` / `full` / `requirements`）保持原样。
- `app/static/app.js`：一键复制仍走同一个入口，成功提示改为「已复制全部沟通与待办，可自行粘贴」。
