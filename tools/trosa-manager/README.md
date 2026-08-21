# trosa Server Manager

Mac 本地服务器管理器。它通过已配置的 Workbench 连接阿里云 ECS，不开放公网管理端口。

## 构建与安装

```bash
tools/trosa-manager/build-macos-app.sh --install
```

程序默认使用 `~/Desktop/Trosa`，也可以在窗口顶部选择其他包含
`deploy/cloud/workbench.env` 的项目目录。

## 当前功能

- ECS、Trade OS、Cloudflare Tunnel 状态和公网健康检查
- 服务重启、ECS 重启、系统更新、服务器终端
- 远程目录浏览、上传、下载、新建目录和 7 天回收站
- Git 状态、提交并发布、GitHub 推送、ECS 发布和版本回滚
- ECS 一致性备份下载到 Mac，并保留 14 天

程序依赖本机已配置的 `workbench`，不会把 Workbench 凭据写入项目或 Git。

安装每日自动备份：

```bash
deploy/macos/install-trosa-backup.sh
```
