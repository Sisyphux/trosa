# trosa 工作台

面向日常使用的 Mac 本地服务器工作台。它通过已配置的 Workbench 连接阿里云 ECS，不开放公网管理端口。

## 构建与安装

```bash
tools/trosa-manager/build-macos-app.sh --install
```

程序默认使用 `~/Desktop/Trosa`，也可以在窗口顶部选择其他包含
`deploy/cloud/workbench.env` 的项目目录。首次打开“文件与资料”时，macOS 可能要求一次性允许访问桌面文件夹；这是读取项目和你主动选择上传文件所必需的系统权限。

## 当前功能

- 概览首页直接显示网站、trosa 应用、安全连接和当前网站版本是否正常
- “上传客户资料”“管理服务器文件”“更新网站”“立即备份”四个日常入口
- 服务重启、整机重启、系统更新、服务器终端和默认收起的技术记录
- 远程目录浏览、上传、下载、新建目录和 7 天服务器回收站
- 一键“保存并同步上线”：本机 Git 提交 → GitHub 同步 → ECS 发布；GitHub 同步失败时不会更新网站
- 一致性备份下载到 Mac，并保留 14 天

程序依赖本机已配置的 `workbench`，不会把 Workbench 凭据写入项目或 Git。

安装每日自动备份：

```bash
deploy/macos/install-trosa-backup.sh
```
