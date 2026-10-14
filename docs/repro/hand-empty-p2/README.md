# 一笔 · 二期（空状态插画）真实浏览器验收

脚本：`tools/repro_hand_empty.cjs`（演示数据服务：`CRM_SEED_DEMO_DATA=1`）。这是外观改动：`before/` 是一期基线下同一位置的截图（旧的一行小字），`after/` 是新的检查报告与截图。

```bash
node tools/repro_hand_empty.cjs --url http://127.0.0.1:<端口> --label after --out docs/repro/hand-empty-p2/after
```

检查项（宽屏、iPhone、减少动态各一遍）：每个空状态有插画且带笔画、有标题、说明不超过两句；同一插画第二次出现不再描线；减少动态下不描线；无横向滚动；无页面错误。演示客户带一条待办，所以“待办空状态”用组件本身渲染检查。
