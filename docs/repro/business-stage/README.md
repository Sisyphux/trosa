# 业务阶段扩到六档

- 现象：Trosa 的业务阶段只有「成交 / 流失 / 留空」，Sela 那边的「回复过 / 寄过样品 / 报过价」没有对应取值；批量改阶段、页面筛选和客户编辑弹窗也都不认识这些阶段。
- 修复：Trosa 用一个统一清单保存和校验六个值（留空 / 回复过 / 寄过样品 / 报过价 / 成交 / 流失），四处下拉和批量弹窗同步；Sela 按整个字段值精确映射，不做子串猜测；迁移 0141 只放宽正式库这一列的取值限制。徽章给三个新阶段加了可区分的低饱和颜色，成交和流失的外观不动。
- 重现脚本：`tools/repro_business_stage.cjs`（用 `tools/repro_dev_server.py` 起隔离开服，不碰业务库）
  - `before/`：修复前源码，退出码 20（2/11）。四个下拉只有成交/流失，新阶段 API 返回 400，列表筛选选不出新阶段，五个徽章全是同一个灰色。
  - `after/`：修复后，退出码 0（11/11）。六个值逐档写入并能从真实详情接口读回，乱值返回 400，客户编辑弹窗载入「报过价」，批量弹窗与筛选可用，三个新徽章颜色两两可分且不同于默认样式，成交/流失保持原样，无页面报错。
- 复跑命令（先把隔离服务起在对应端口）：

```bash
node tools/repro_business_stage.cjs --url http://127.0.0.1:18192 --label before --out docs/repro/business-stage/before
node tools/repro_business_stage.cjs --url http://127.0.0.1:18191 --label after --out docs/repro/business-stage/after
```

- `report.json` 保存了每一项的详细实测值；`01-signed-in.png`、`02-customer-editor.png`、`03-badge-colours.png` 是真实浏览器截图。
