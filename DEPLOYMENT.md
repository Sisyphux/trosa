# Trade OS 上线指南（家庭服务器 + Cloudflare Tunnel）

云服务器部署补充流程见 [`deploy/cloud/README.md`](deploy/cloud/README.md)。云端仍保持单台主机、单个 SQLite 写入进程；Workbench 用于受控查看、发布已推送的代码和执行 systemd 运维命令。

## 日常代码发布

当前推荐流程是在服务器工作台的“更新网站”页面点击“保存并同步上线”。它会先把当前修改提交到本地 Git，再推送到 `Sisyphux/trosa`，然后通过 ECS Session Manager（SSM）让云服务器下载并发布同一个 commit，最后检查应用健康状态。这样 GitHub 和正式服务器保持同一版本。

单独执行 `git push` 或使用 GitHub Desktop 的 Push，只会更新 GitHub，不会自动更新 ECS。手动推送后，在同一个本地项目目录运行：

```bash
deploy/cloud/publish-workbench.sh
```

该脚本只发布本地 `HEAD` 对应的、已经推送到公开 GitHub 仓库的 commit；它不会发布未提交的文件。当前仓库没有 GitHub Actions 或 Webhook，因此“推送后自动发布”目前指服务器工作台的“保存并同步上线”流程，而不是任意 GitHub Push 事件触发的云端流水线。

当前规模适合以一台长期运行的主机承载单个应用进程。当前正式主机为阿里云 ECS；应用通过 Cloudflare Tunnel 提供公网入口。公司 Mac 不再运行第二套 Trade OS，只在连接公司网络并取得固定地址 `192.168.0.58` 时，自动提供一个读取云端周报的局域网入口。另准备一台备用 Mac 或 NAS 作为冷备机，用于故障后的恢复切换。

## 运行角色

- **主机（当前 ECS）**：唯一允许运行 Trade OS 与写入 `CRM_DB_PATH` 的设备，也是唯一运行 Cloudflare Tunnel 的设备。
- **公司 Mac**：不保存、不合并、不写入业务数据库；只在公司网络上提供 `http://192.168.0.58:8080` 只读周报入口。
- **备用机**：保留同版本代码、Python 环境、`cloudflared` 安装与不含密钥的环境模板；平时不启动 Trade OS，不挂载或同步正在使用的数据目录。
- **备份副本**：每天从主机生成带校验清单的 SQLite 快照，再加密复制到外接 SSD 和一处异地存储。Time Machine 可作为整机恢复补充，不能替代这份可独立校验的业务备份。

SQLite 不支持两台主机同时写同一数据目录。发生主机故障时，先确认主机已停止，再在备用机恢复最近一次校验通过的快照、更新 Tunnel 运行位置并启动服务；恢复后的备用机成为新的唯一主机。

## 上线前

### 公司局域网只读周报入口

正式应用和 SQLite 全部留在 ECS。公司 Mac 安装 `com.tradeos.weekly-lan` 后会常驻等待网络变化：只有本机真正取得 `192.168.0.58` 时才监听 8080；离开公司、关机或失去该地址后入口自动消失。它只转发应用外壳、周报汇总和周报客户详情，任何写入请求及其他 CRM 接口都在 Mac 和 ECS 两端拒绝。

1. 确认公司路由器将 `192.168.0.58` 固定分配给这台 Mac，且没有把 8080 转发到互联网。
2. 在 Mac 项目目录执行 `deploy/macos/install-weekly-lan.sh`。首次安装会创建仅本机可读的随机密钥，并输出对应的 SHA-256 摘要。
3. 将输出的 `CRM_WEEKLY_GATEWAY_TOKEN_SHA256=...` 安全写入 ECS 的 `/etc/trade-os/trade-os.env`，重启 `trade-os`。ECS 只保存摘要，不保存 Mac 使用的原始密钥。
4. 在非公司网络确认 Mac 不监听 8080；回到公司后从另一台设备打开 `http://192.168.0.58:8080`，确认自动进入本周工作。再验证普通客户接口与 POST 请求均被拒绝。

安装文件位于 `~/Library/Application Support/TradeOS/`，私密配置为 `weekly-lan.env`，日志位于 `logs/weekly-lan.log` 和 `logs/weekly-lan-error.log`。该入口不依赖旧的 `com.tradeos.app`、`com.tradeos.tunnel` 或 `com.tradeos.health`；这三个 Mac 正式服务继续保持禁用，避免与 ECS 双运行。

### 旧 Mac 正式服务（仅用于灾难回退）

当前项目提供 `deploy/macos/` 中的正式运行文件：`run-production.sh` 负责读取仅本机可见的生产设置，`com.tradeos.app.plist.example` 用于登录后自动启动服务。正式环境安装在 `~/Library/Application Support/TradeOS/runtime/`，避开 macOS 对桌面目录的自动启动限制；原项目的 `data/` 指向该目录中的唯一业务数据。它们只会在最终切换时启用，准备期间继续使用现有本地启动器。

- 私有生产设置必须设置会话密钥和 `https://app.trosa.space`；三位用户的不同 6 位访问码在生产登录页首次进入时分别设置，并只以哈希形式保存在 `CRM_DB_PATH/system.db`。
- 只有执行灾难回退并让 Mac 再次成为唯一正式主机时，才设置 `CRM_BIND_HOST=0.0.0.0` 与 `CRM_INTERNAL_VIEWER_CIDRS=192.168.0.0/23`。ECS 正常运行期间不得启动这套旧服务。
- 生产服务将唯一业务数据保存在 `~/Library/Application Support/TradeOS/runtime/data/`；项目根目录的 `data/` 仅为指向该位置的链接，不复制、不合并、不创建第二份日常数据库。
- 日常开发始终在桌面项目目录完成。完成并验证修改后，运行 `deploy/macos/publish-production.sh`，它会同步代码和静态资源、重启正式服务并检查本机健康状态；不会同步或删除 `data/`、私密设置、日志或 Python 运行环境。
- LaunchAgent 适合当前由 Mac 登录用户持续使用的场景。正式运行时 Mac 需接通电源、保持联网和用户登录。`run-production.sh` 使用 macOS 自带的 `caffeinate -i` 阻止**空闲系统睡眠**，但不阻止显示器熄灭，因此屏幕关闭后本机服务与 Cloudflare Tunnel 仍保持在线。用户从菜单手动选择“睡眠”或机器断电时服务仍会短暂离线；唤醒后健康检查会恢复连接。

### 公网可用性自检与自动恢复

生产环境提供独立于应用进程的自检器，用于处理 Cloudflare 1033（Tunnel 暂无活动连接）及本机服务未监听等临时故障。它不会修改代码、SQLite 数据、私密环境文件或 Tunnel 凭据。

1. 在项目目录执行一次安装：

   ```bash
   deploy/macos/install-health-monitor.sh
   ```

   安装完成后，`com.tradeos.health` 会在用户登录时执行一次，并每分钟检查本机 `/api/network/ping` 与公网 `https://app.trosa.space/api/network/ping`。本机检查失败时重启 `com.tradeos.app`；公网检查连续两次失败时重启 `com.tradeos.tunnel`，随后等待恢复确认。

2. 需要即时检查时，可双击项目根目录的 `Trade OS 上线自检与修复.command`，或直接运行：

   ```bash
   ~/Library/Application\ Support/TradeOS/check-and-repair.sh
   ```

3. 自检只在发生异常或恢复动作时写入 `~/Library/Application Support/TradeOS/logs/health-monitor.log`。若连续恢复失败，可查看该日志以及 `logs/trade-os-tunnel-error.log`，确认本机网络仍可访问 Cloudflare Tunnel 的 7844 端口。

每次正常发布会将已安装的自检脚本更新到生产目录；如需修改检查频率或 LaunchAgent 路径，更新 `deploy/macos/com.tradeos.health.plist.example` 后重新运行安装脚本。

1. 创建专用系统账号、项目目录和 Python 虚拟环境：

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. 将 `.env.example` 复制为 `/etc/trade-os/trade-os.env`，至少设置：

   ```ini
   CRM_ENV=production
   CRM_SESSION_SECRET=至少32字符的随机值
   CRM_PUBLIC_URL=https://trade.example.com
   CRM_DB_PATH=/var/lib/trade-os
   ```

   使用 `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` 生成会话密钥，并将环境文件权限设为 `600`。用户首次进入生产登录页时创建自己的 6 位访问码；本地开发模式仍可选择账号后直接进入。

3. AI、官网监控和邮箱 SMTP 复核均为可选能力。需要时再在环境文件中加入相应密钥和配置。核心 CRM 上线验收不要求模型密钥。

4. 将 `deploy/trade-os.service` 复制到 `/etc/systemd/system/`，按实际账号、项目目录、虚拟环境和数据目录调整：

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now trade-os
   sudo systemctl status trade-os
   curl http://127.0.0.1:8080/api/network/ping
   ```

5. 创建 Cloudflare Named Tunnel，修改 `deploy/cloudflared-config.yml.example` 中的域名和 Tunnel UUID，并把 cloudflared 注册为系统服务。

6. 在 Cloudflare Zero Trust 中创建 Access Application，只允许三位团队成员邮箱，并为 Tunnel 开启“Protect with Access”。应用访问码继续保留，形成两层访问控制。

## 数据目录

`CRM_DB_PATH` 指向唯一可写的业务数据目录，里面包含用户数据库、系统数据库、导入来源、仓库标识和本地快照。

- 数据目录使用本机持久磁盘。
- 同一数据目录只允许一个 Trade OS 应用进程写入。
- 数据目录不放进 iCloud、Dropbox 或其他文件级实时同步文件夹。
- 代码更新与数据迁移分开操作。

## 备份和恢复

应用保持一个唯一可写主数据库，不会把本机快照当作第二个运行中的数据库。除写入后的安全快照外，正式服务的定时调度器每天 **02:15（Asia/Shanghai）** 创建一个独立的本机历史快照；即使当天没有客户修改，也会产生恢复点。快照包含 `system.db`、三位成员数据库和客户附件，写入清单并校验 SHA-256 与 SQLite `integrity_check`。任务错过后，应用重启可在 7 天内补执行；任务失败只记录失败状态，不切换主库。

本机快照用于快速恢复误删和错误操作，但与主数据库仍在同一台 Mac、同一数据目录下，不能抵御整机或磁盘损坏。正式上线还需要异地备份：

1. 每天把 `CRM_DB_PATH/backups/` 加密复制到外接盘或可信对象存储。
2. 至少保留 30 天，建议与应用的 90 天本地保留策略配合。
3. 每月在测试目录恢复一次。
4. 恢复前先保留当前版本；恢复后核对数据库完整性、客户数、最近沟通和待办。
5. `data/`、`.env` 和备份文件均不得提交到 Git。
6. 每季度在备用机完成一次真实演练：从异地副本恢复、启动服务、以测试地址登录并检查三位用户的数据与 ICS 链接；演练结束后关闭备用机服务，避免双写。

## 上线验收

- 公网 HTTPS 域名可用，家庭公网 IP 和 8080 端口未直接暴露。
- Cloudflare Access 只接受授权邮箱。
- 三位用户分别使用自己的访问码登录，数据互相隔离。
- 在未配置模型密钥的情况下，可创建客户、记录沟通、安排待办、处理 Inbox、导入 Excel、打开日历并创建恢复快照。
- Apple 日历个人订阅只返回对应用户的未完成待办。
- 重启服务器后 `trade-os` 与 `cloudflared` 自动恢复。
- 异地备份已完成一次真实恢复演练。
- 主机断电或故障后，备用机可依据最近一次校验通过的快照恢复为唯一主机；没有两台主机同时运行或写入同一数据目录。
- 如启用 AI、官网监控或 SMTP 复核，再单独验收其网络、密钥、失败降级和数据范围。

## 当前容量边界

SQLite 与单进程服务适合当前三人低并发使用。出现高频同时编辑、十几位以上用户、异地高可用或多应用实例需求时，应迁移到 PostgreSQL，并重新设计任务锁、会话和备份策略。
