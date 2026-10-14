# 一笔 · 一期（图标与等待）真实浏览器验收

脚本：`tools/repro_hand_icons.cjs`。这是一次外观改动，不是缺陷修复：`before/` 是基线（改动前的树），`after/` 是改动后。

```bash
node tools/repro_hand_icons.cjs --url http://127.0.0.1:<端口> --label after --out docs/repro/hand-icons-p1/after
```

- `before/report.json`：30 项检查失败（基线没有手绘图标、没有等待组件、没有状态条画勾）——脚本能区分新旧。
- `after/report.json`：全部通过。
- 六种情形各有截图：宽屏 1440、iPad 820、iPhone 390、200% 缩放（720 逻辑宽）、减少动态、键盘（含焦点）。每种情形：今天、导航、保存中、已保存、等待中、客户列表。

检查项：导航里 3 个手绘图标（带浅灰片）；遮罩图标都解析到 `icons/hand/`；无横向滚动；悬停描线一次（减少动态下不描）；提示起始位移 ≤ 8px；等待组件 120ms 内不出现、800ms 后出现；减少动态下没有循环动画；全程无页面错误。
