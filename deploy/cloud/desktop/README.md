# 私有浏览器桌面

这一组文件在 Ubuntu 上运行一个最小化 XFCE 桌面（包含 `xfdesktop4`，用于桌面背景和图标）：TigerVNC 只监听服务器的
`127.0.0.1:5901`，noVNC 只监听 `127.0.0.1:6080`。它们不开放公网端口，也不加入
Cloudflare Tunnel。

访问流程是：Mac 的专用 SSH 密钥 -> 本机 SSH 隧道 -> `http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale`。
当前按用户明确选择没有另设 VNC 密码；远程桌面本身只能由持有这台 Mac 私钥的账户访问。不要将 5901 或 6080
加入公网安全组、UFW 或 Cloudflare Tunnel；如果未来允许其他设备访问，必须先启用独立 VNC/noVNC 认证。

`trosa-desktop` 是独立、无 sudo 权限的系统账户。它不读取 Trosa 的程序目录、数据库
或 Cloudflare 配置。桌面服务可随时执行以下命令停用，不影响 Trosa：

```bash
systemctl disable --now trosa-novnc.service trosa-vnc.service
```

桌面使用 Google Chrome Stable 作为唯一默认图形浏览器，支持中文、现代 JavaScript 和持续安全更新。它使用
独立的 `trosa-browser` profile，禁用会话恢复；因此每次只从一个干净浏览器工作区开始。桌面画布固定为
1440×900（16:10，接近 Mac 侧边栏浏览时的可视比例），浏览器采用等比缩放，不使用 TigerVNC 的远程动态调分辨率。
`mimeapps.list` 负责 XFCE/GTK 的默认浏览器关联，`helpers.rc` 负责 XFCE 面板/应用菜单使用的默认浏览器入口。

桌面左上角的 “Trosa 浏览器” 图标会打开 Trosa，底部的蓝色地球图标可用于普通网页浏览。不要同时打开很多浏览器窗口，避免挤占这台小型服务器的资源。

## 部署与验收

将本目录的两个 `.service` 文件放入 `/etc/systemd/system/`，将 `xstartup` 放入
`/home/trosa-desktop/.vnc/xstartup` 并设为该用户所有；将 `trosa-browser` 放入
`/usr/local/bin/` 并设为可执行文件；将 `trosa-chrome.desktop`、`trosa-chrome-helper.desktop`、
`mimeapps.list` 和 `helpers.rc` 分别放到该用户的应用、XFCE helper 与配置
目录；将 `trosa-web.desktop` 放入 `/home/trosa-desktop/Desktop/`，并将
`chrome-policy.json` 放入 `/etc/opt/chrome/policies/managed/trosa.json`，然后执行：

```bash
systemctl daemon-reload
systemctl enable --now trosa-vnc.service trosa-novnc.service
ss -ltnp | rg '5901|6080'
```

验收标准是两个端口均只显示 `127.0.0.1`；Trosa 与 cloudflared 服务均保持 active。
