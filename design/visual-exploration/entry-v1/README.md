# Trosa — 入口页视觉探索 v1

四个从零设计的「账号选择 / 进入 Trosa」视觉原型。均为独立、可离线打开的 HTML，不接后端、不改动现有前端代码。

| 文件 | 概念 | 一句话 |
| --- | --- | --- |
| `01-index.html` | Index · 编辑目录 | 不对称杂志目录：衬线大字、编号索引、暖纸色 + 陶土点缀 |
| `02-drafting.html` | Drafting · 制图网格 | 坐标纸与制图线：不是卡片而是一个非对称格网，等宽字 + 冷灰蓝 |
| `03-dial.html` | Dial · 环绕刻度 | 环形刻度盘 + 主从版式：环绕节点，左侧大字随焦点变化，苔绿 |
| `04-colonnade.html` | Colonnade · 竖列色场 | 满屏四条色场竖列：静止时是竖排书脊，悬停展开，泥土色系 |

每个原型都支持：悬停 / 键盘 `↑↓←→`、`1`–`4` 直接选择、`Enter` 进入 workspace、`Esc` 返回、`H` 隐藏角标。

## 预览

- 对比页：`index.html`（2×2 同时显示四套，可切换 Desktop / iPad / iPhone 真实断点）
- 稳妥方式：双击 `preview.command`（本地起临时服务后打开对比页）

```bash
# 手动方式
cd design/visual-exploration/entry-v1 && python3 -m http.server 8787
# 然后打开 http://127.0.0.1:8787/index.html
```
