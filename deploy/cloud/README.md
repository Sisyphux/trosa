# ECS + Workbench 发布流程

Trade OS 使用一台持久磁盘 ECS、一个 Waitress 进程、一个 PostgreSQL 写入源和一个 Cloudflare Tunnel。不要启动第二个 Trade OS 实例，也不要把 `data/`、`.env` 或 `.venv` 上传到发布包。ECS 主机防火墙只允许 SSH，应用只监听 `127.0.0.1:8080`；PostgreSQL 只监听 ECS `127.0.0.1:5432`。

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

当前正式入口为 `https://app.trosa.space`。正式业务数据位于 ECS 的
PostgreSQL（`/opt/trade-os-postgres`），应用通过 systemd drop-in 注入的
DSN 连接；`/var/lib/trade-os` 仅保存客户附件、导入来源和历史回滚材料。Tunnel
凭据只保存在 `/etc/cloudflared/`，不会进入代码仓库。

## 与 sela 的稳定同步契约

云端发布后，sela 通过 Hamid 专用 Bearer 令牌访问以下接口：

- `GET /api/integrations/sela/health`：轻量健康检查和契约版本；
- `GET /api/integrations/sela/exclusions`：带 ETag 的排除索引，不传输整张客户表；
- `POST /api/integrations/sela/sync`：单条已确认外联事件的事务写入。

`sela-v1` 的写入接口会在一个 PostgreSQL 事务内完成精确身份匹配、联系人、来源备注和真实外联时间线，并保存 `X-Idempotency-Key` 回执。sela 在网络超时后可以安全重放同一事件，不会重复创建客户或开发信；官网身份按完整规范化域名比较，不使用子串匹配。多重命中、外部身份冲突和邮箱属于另一客户时会返回 `REVIEW`，由人工处理。

发布和验收顺序：

1. 先通过 `deploy/cloud/publish-workbench.sh` 发布包含 `sela-v1` 的 Trosa commit。服务启动时会自动执行增量数据库迁移，既有数据不会被重建。
2. 在 Mac 的 sela 项目中运行 `python3 tools/lab.py trosa-status`，确认结果中的 `sync_api` 为 `sela-v1`，再运行 `python3 tools/lab.py trosa-retry` 处理历史积压。
3. 观察 `data/trosa_auto_sync_state.json` 和 `data/server.log`；只有成功同步的 candidate 才会从 retry queue 消失，`review` 必须人工确认。

如果发布健康检查失败，发布脚本会切回上一份 release；若已经有新契约写入数据，不要把旧代码作为长期运行版本，应重新发布包含 `sela-v1` 的版本。数据库新增字段和回执表是向后兼容的，发布前不需要停掉本地 sela。

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

## Hamid Pi CRM 助理

Pi 是 Trosa Agent Gateway 的 CRM 调用方。Gateway 只验证 `hamid-pi`、绑定 Hamid 数据并记录 CRM 操作；不限制 Pi 的原生 shell、文件、搜索、编码或调试能力。首次启用先在 ECS 安装锁定版本的 Node 与 Pi：

```bash
sudo /opt/trade-os/current/deploy/cloud/install-pi-runtime.sh
```

然后由已登录的 Hamid 在 Trosa 的「Agent Gateway token」页面新建一个含
`crm:read,crm:write` 的 `hamid-pi` token。把 token 和下列运行参数写入
`/etc/trade-os/trade-os.env`（权限保持 600），再重启 `trade-os`：

```text
TROSA_PI_AGENT_ENABLED=true
TROSA_PI_EXECUTABLE=/opt/trade-os/pi-runtime/npm/bin/pi
TROSA_PI_PROVIDER=deepseek
TROSA_PI_MODEL=deepseek/deepseek-v4-flash
TROSA_PI_HOME=/var/lib/trade-os/pi-home
TROSA_GATEWAY_URL=http://127.0.0.1:8080
TROSA_PI_GATEWAY_TOKEN=<Hamid 的新 token>
```

不要把 token 写入仓库、release、命令历史或 sela。Gateway token 的明文唯一来源是 `/etc/trade-os/trade-os.env`；数据库只保存 SHA-256 hash。模型凭证唯一来源是 `/var/lib/trade-os/ai-config.env`，由 Trosa AI 配置加载并传给 Pi。验证必须先用 Pi 读取真实 Today，再由用户提供一条真实业务事实做一次可撤销写入；不得用虚构客户沟通作为生产验收材料。

`status-workbench.sh` 会先输出 `TROSA_MANAGER_STATUS` 和
`TROSA_MANAGER_RESOURCE` 两行稳定字段，分别供工作台读取服务可用性、`sela` 同步契约版本以及
CPU、内存、根分区磁盘、负载和运行时间；后面的 systemd、磁盘和日志内容仍用于技术排查。
只读状态检查优先走 Workbench 控制面，即使本地配置了 SSH 主机别名；这样本机 SSH
暂时无法握手时，工作台仍有机会读取网站和服务器状态。如果 Workbench 非交互请求临时失败，脚本会先使用已配置的 SSH
主机别名回退，再尝试旧版 Workbench 的交互式会话，避免把控制面短暂错误显示成服务器故障。
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

`backup-workbench.sh` 会调用 ECS PostgreSQL 生产目录的 verified logical dump，核对 dump
的 SHA-256 和 `pg_restore --list`，再把数据库 dump、客户附件和 manifest 打包下载到 Mac 的
`~/Library/Application Support/trosa/backups/`，核对 bundle SHA-256 后保留最近 14 天的归档。
它不创建阿里云 ECS 系统盘快照；系统盘级灾难恢复需要另外配置云快照或重建 ECS。应用在
PostgreSQL 模式下不会把旧 SQLite 目录伪装成备份，也不会通过备份 API 恢复 SQLite 文件。

## 私有浏览器桌面

服务器还运行一个独立的轻量 XFCE 桌面，供必要时在浏览器中直接操作 Ubuntu。它使用
`trosa-desktop` 无 sudo 权限账户，与 Trosa 的 `tradeos` 数据账户隔离；TigerVNC 与 noVNC
只监听服务器 `127.0.0.1:5901/6080`，没有 ECS 公网端口、UFW 规则或 Cloudflare Tunnel 路由。

从 Mac 通过 `trosa-desktop` SSH 主机别名打开本机隧道，再访问
`http://127.0.0.1:6080/vnc.html`。服务器工作台已将此动作做成“打开服务器桌面”。服务文件、
停用方式和验收要求见 [desktop/README.md](desktop/README.md)。

数据库仍是单写入源。不要在 Mac 上重新启动旧的正式应用并通过同一个域名使用；当前 Mac 的三个旧 LaunchAgent 已禁用但文件保留。如需回退到 Mac，先停止 ECS Tunnel，再执行：

```bash
USER_ID=$(id -u)
for label in com.tradeos.app com.tradeos.tunnel com.tradeos.health; do
  launchctl enable "gui/$USER_ID/$label"
  launchctl bootstrap "gui/$USER_ID" "$HOME/Library/LaunchAgents/$label.plist"
done
```
