# 选账号界面竖排小字和大字重叠

- 日期：2026-11-11
- 浏览器：chromium 153.0.8010.12（Playwright，由 `browser-extension/package-lock.json` 锁定）
- 复现脚本：[`tools/repro_entry_overlap.cjs`](../../../tools/repro_entry_overlap.cjs)

| 运行 | 结果 | 退出码 |
| --- | --- | --- |
| `before/`（修复前） | 打开后 0.4–1.1 秒，鼠标所在栏的竖排名字和大字同时可见 | 20 |
| `after/`（修复后） | 各采样点都没有重叠 | 0 |

## 根因

选账号界面打开时，竖排名字播放入场动画 `entrySpineIn`（[`app/static/style.css`](../../../app/static/style.css)）。动画播放期间会盖过选中栏 `opacity: 0` 的规则，于是鼠标所在那一栏的竖排小字和展开后的大字叠在一起，约 1 秒后动画结束才消失。

## 修改

选中栏的竖排名字同时把文字颜色设为透明（动画不改颜色），颜色也加入过渡，移到别的栏时竖排名字照常淡入回来。
