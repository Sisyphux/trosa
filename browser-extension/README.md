# Trade OS 沟通采集扩展

这是一个可解压加载的 Chrome/Edge Manifest V3 初版。打开 `chrome://extensions` 或 `edge://extensions`，启用“开发者模式”，选择“加载已解压的扩展程序”，指向本目录。

扩展点击后打开 Side Panel；不支持 Side Panel 的浏览器可从扩展 popup 进入降级说明。首次使用在侧栏填写 Trade OS 服务地址和账号访问码。默认服务地址为正式入口 `https://app.trosa.space`；本地开发时可改为 `http://localhost:8080`。

如需让扩展跨域调用正式服务，加载扩展后复制 `chrome://extensions` 中显示的扩展 ID，将 `chrome-extension://扩展ID` 写入服务器 `/etc/trade-os/trade-os.env` 的 `CRM_CORS_ORIGINS`，再执行 `sudo systemctl restart trade-os`。不使用扩展时保持为空。

采集范围严格限制为用户当前打开的页面：网易邮箱取当前线程可识别邮件，WhatsApp Web 取当前页面已加载消息。扩展不会访问 WhatsApp 私有数据库、私有网络请求或发送消息；保存前必须确认客户、联系人、消息和可编辑字段。

开发验收可运行：

```bash
cd browser-extension
npm install
npm test
```

侧栏右上角的“✓”会运行当前页面验收：检查页面授权、每个可访问 frame 的内容脚本通信与自动恢复、消息数量、Trade OS 登录状态和可修改归属能力。报告不包含邮件正文、邮箱或手机号；遇到问题时复制该报告即可定位失败层级。

若浏览器中已通过开发者模式加载本扩展，并以 `--remote-debugging-port=9333` 启动隔离测试配置，还可运行 `edge-integration.cjs` 完成 MV3 Service Worker → frame 内容脚本端到端验收。新版官方 Chrome/Edge 已不保证接受命令行 `--load-extension`，因此需先在扩展管理页手动“加载已解压的扩展程序”。
