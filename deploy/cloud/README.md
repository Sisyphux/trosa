# ECS + Workbench 发布流程

Trade OS 使用一台持久磁盘 ECS、一个 Waitress 进程、一个 SQLite 写入源和一个 Cloudflare Tunnel。不要启动第二个 Trade OS 实例，也不要把 `data/`、`.env` 或 `.venv` 上传到发布包。ECS 主机防火墙只允许 SSH，应用只监听 `127.0.0.1:8080`。

首次配置：

```bash
cp deploy/cloud/workbench.env.example deploy/cloud/workbench.env
# 编辑实例 ID 等路由信息，不要写入任何密钥
deploy/cloud/bootstrap-workbench.sh
```

首次配置还需要由维护者安全写入 `/etc/trade-os/trade-os.env`、`/etc/cloudflared/config.yml` 和 Tunnel 凭据，然后再启用：

```bash
sudo systemctl enable --now trade-os cloudflared
```

当前正式入口为 `https://app.trosa.space`。生产数据位于 ECS 的
`/var/lib/trade-os`，由 `tradeos` 用户独占写入；Tunnel 凭据只保存在
`/etc/cloudflared/`，不会进入代码仓库。

公司局域网继续使用 `http://192.168.0.58:8080` 查看只读周报，但该地址现在由公司 Mac 上的 `com.tradeos.weekly-lan` 提供。Mac 只把允许的周报读取请求转到本 ECS，并用独立随机密钥证明来源；ECS 环境只保存 `CRM_WEEKLY_GATEWAY_TOKEN_SHA256` 摘要。Mac 不运行第二个 Trade OS、不读取本地旧数据库，离开公司网络后也不会监听该地址。安装和验收步骤见根目录 `DEPLOYMENT.md`。

代码同步与云端发布是两个动作：单独执行 `git push` 或在 GitHub Desktop 点击 Push，只会更新 GitHub，不会触发 ECS。日常开发应在服务器工作台的“更新网站”页面点击“保存并同步上线”，由工作台按“提交当前修改 → 推送 GitHub → 通过 SSM 发布同一个 commit → 健康检查”的顺序完成。若已经从编辑器或 GitHub Desktop 手动推送，则再运行 `publish-workbench.sh`，它会发布当前本地 `HEAD`；当前仓库是公开的 `Sisyphux/trosa`，因此 ECS 可以直接下载对应 commit 的 GitHub 归档。仓库目前没有配置 GitHub Actions 或 Webhook 自动部署。

日常操作：

```bash
deploy/cloud/status-workbench.sh
deploy/cloud/logs-workbench.sh
deploy/cloud/publish-workbench.sh
deploy/cloud/rollback-workbench.sh
deploy/cloud/backup-workbench.sh
```

`status-workbench.sh` 会先输出 `TROSA_MANAGER_STATUS` 和
`TROSA_MANAGER_RESOURCE` 两行稳定字段，分别供工作台读取服务可用性、版本以及
CPU、内存、根分区磁盘、负载和运行时间；后面的 systemd、磁盘和日志内容仍用于技术排查。
当目标实例无法通过 SSH 连接、Workbench 降级到 Session Manager（SSM）模式时，脚本会
通过一次短生命周期的交互式 Shell 读取同样的状态；这是因为 Workbench 的 SSM 模式不支持
非交互式 `exec`。会话结束后立即关闭，不保留后台终端。

发布前建议先查看状态；发布失败时脚本会自动回退，手动回退使用：

```bash
deploy/cloud/status-workbench.sh
deploy/cloud/publish-workbench.sh
deploy/cloud/rollback-workbench.sh
deploy/cloud/logs-workbench.sh
```

`publish-workbench.sh` 会读取本地当前 `HEAD` 和 GitHub `origin`，让 ECS 通过 SSM 下载该 commit 的公开归档，解压到新的 release 目录，安装依赖，执行 Python 语法检查，原子切换 `current` 符号链接，重启服务并验证本机健康接口；失败时会自动切回上一个 release。发布包来自已推送的 commit，不包含本机未提交修改，也不上传本地数据、密钥或虚拟环境。

`backup-workbench.sh` 会在 ECS 创建一致性快照，打包数据库和附件，下载到 Mac 的
`~/Library/Application Support/trosa/backups/`，核对 SHA-256 后保留最近 14 天的归档。

数据库仍是单写入源。不要在 Mac 上重新启动旧的正式应用并通过同一个域名使用；当前 Mac 的三个旧 LaunchAgent 已禁用但文件保留。如需回退到 Mac，先停止 ECS Tunnel，再执行：

```bash
USER_ID=$(id -u)
for label in com.tradeos.app com.tradeos.tunnel com.tradeos.health; do
  launchctl enable "gui/$USER_ID/$label"
  launchctl bootstrap "gui/$USER_ID" "$HOME/Library/LaunchAgents/$label.plist"
done
```
