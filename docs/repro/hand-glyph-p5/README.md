# 一笔 · 五期（图标优先第二版）真实浏览器验收

脚本：`tools/repro_hand_glyph.cjs`（演示数据服务：`CRM_SEED_DEMO_DATA=1`）。

```bash
node tools/repro_hand_glyph.cjs --url http://127.0.0.1:<端口> --label after --out docs/repro/hand-glyph-p5/after
```

核心要求：**任何图标 / 符号，鼠标放上去或键盘聚焦都显示功能名称。** 检查项包括：提示层出现且文字正确、移开后消失、原生 `title` 被接管；分区标签当前显示名称、其余只留图标、触屏保留名称；状态符号带名称；数字被“写上去”（减少动态下不播）；推迟 / 归档的划线在失败时撤回。

- `hover-tabs.png`：悬停分区图标显示“联系人”。
- `hover-readouts.png`：悬停读数图标显示名称。
- `status-glyphs.png`：台账里的状态符号（沙漏 = 等待回复，虚线圈 = 尚无 / 未获回复，三角 = 逾期，悬停显示“逾期 3 天”）。
