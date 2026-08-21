# Trade OS 审美积累库

这里存放未来界面工作可直接参考的截图、网页摘录、组件细节、色彩与材质样本，以及需要主动避开的反例。

现有视觉原则以 [`../TRADE_OS_UI_SYSTEM.md`](../TRADE_OS_UI_SYSTEM.md) 为准。本库提供证据和灵感，不会自动改变产品的功能、信息架构或设计系统。

## 投放方式

素材先放入 `00-inbox/`；一张图、一个链接摘录或一个文件放一份。收集完成后，可按用途整理进对应目录，并在 [`CATALOG.md`](CATALOG.md) 登记。

文件名使用清楚的英文短横线命名：

```text
linear-issue-list-density.png
notion-sidebar-hierarchy.png
apple-settings-mobile-grouping.jpg
avoid-gradient-dashboard-overload.png
```

保留来源信息。可用同名 `.md` 记录来源链接、页面状态、可借鉴部分和不采用部分；截图本身不应包含客户姓名、邮箱、电话、密钥或其他敏感数据。

## 目录说明

| 目录 | 放什么 | 未来如何使用 |
| --- | --- | --- |
| `00-inbox/` | 未分类的新素材 | 先收集，后整理 |
| `01-layout-and-density/` | 信息层级、网格、留白、列表密度 | 页面结构与排版 |
| `02-navigation-and-workflows/` | 导航、搜索、工作流、空状态 | 页面之间的路径与操作入口 |
| `03-components-and-interactions/` | 按钮、表单、弹窗、反馈与微交互 | 组件样式和状态 |
| `04-typography-colour-material/` | 字体、颜色、纸张/玻璃材质、图标 | 视觉令牌与表面语言 |
| `05-mobile-and-responsive/` | 窄屏布局、触控和断点处理 | 响应式验收 |
| `06-anti-patterns/` | 希望避免的视觉模式 | 设计审查时的反例清单 |
| `_archive/` | 已失效、已替代或不再适用的素材 | 保留历史，不参与默认参考 |

## 使用约定

每次界面设计或改版前：

1. 先读 `../TRADE_OS_UI_SYSTEM.md`。
2. 查阅与任务相关的目录和 `CATALOG.md`。
3. 明确素材中“可借鉴”与“排除”的部分，再开始实现。
4. 完成后以本库和 UI 系统为依据进行截图审查。

高价值素材优先整理成可复用的设计结论，例如“客户列表在 1440px 下的合理信息密度”，而非只保留图片。
