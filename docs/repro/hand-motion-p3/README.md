# 一笔 · 三期（动作回应与每页点睛）真实浏览器验收

脚本：`tools/repro_hand_motion.cjs`（演示数据服务：`CRM_SEED_DEMO_DATA=1`）。外观改动，没有缺陷基线；`after/` 是检查报告与截图。

```bash
node tools/repro_hand_motion.cjs --url http://127.0.0.1:<端口> --label after --out docs/repro/hand-motion-p3/after
```

检查项（宽屏、iPhone、减少动态各一遍）：今天焦点客户名、台账书眉分段名、“一句下一步”下各有一笔；新记录出现墨迹并在约 2 秒后移除（减少动态下不出现）；点“标记完成”立刻划掉、按钮上是笔、保存失败后撤回；按钮成功时是画出来的勾；sela 处理中显示笔在写；无横向滚动、无页面错误。
