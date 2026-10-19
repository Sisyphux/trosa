# 标色菜单点不动

- 场景：编辑沟通记录弹窗里双击选词 → 点标色菜单黄色。
- 根因：`syncModalInert` 把弹窗外的 body 子元素全部设为 inert，`#highlightContextMenu` 也在其中，鼠标点击穿透到编辑框。
- 重现：`node tools/repro_highlight_menu.cjs --label before --out docs/repro/highlight-menu/before`（需 `tools/repro_dev_server.py` 隔离服务）。
- 结果：`before/` 4 项中 2 项失败（退出 20）；`after/` 4 项全部通过（退出 0），Chromium 与 WebKit 均验证。
