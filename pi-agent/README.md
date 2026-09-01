# Trosa Pi Agent runtime

这是 Trosa 聊天入口使用的 Pi 运行时配置，不是 MCP，也不直接访问 SQLite。

`trosa-tools.ts` 是 Pi 扩展：CRM 读写全部走已认证的 `/api/gateway/*`，本地文件只通过受边界保护的只读工具读取。Flask 负责为 Hamid 聊天请求生成隔离的 Pi 会话文件，并从 Pi JSON 事件中提取最终回复和 action id/Undo 信息。

## 运行时配置

- `TROSA_PI_AGENT_ENABLED=true`：启用聊天入口的 Pi runtime；未设置时保留受限本地回退。
- `TROSA_PI_EXECUTABLE=/usr/local/bin/pi`：Pi 可执行文件绝对路径，默认从 PATH 查找。
- `TROSA_PI_PROVIDER=deepseek`、`TROSA_PI_MODEL=deepseek/deepseek-v4-flash`：模型选择。密钥由 Pi 的认证文件或对应 provider 的环境变量提供，不写入仓库。
- `TROSA_PI_HOME=/var/lib/trade-os/pi-home`：可选的 Pi 认证/缓存目录。ECS 的 systemd 启用了 `ProtectHome`，生产环境应把 Pi 的认证文件放在这个由 `tradeos` 用户独占的目录，而不是用户家目录。
- `TROSA_PI_SESSION_DIR=/var/lib/trade-os/pi-sessions`：Pi 会话目录，由 `tradeos` 用户独占。
- `TROSA_PI_WORKFILES_ROOT=`：可选的只读本地工作文件夹。Hamid 的 CRM 助理默认不注册任何文件工具；只有经过单独审查后同时设置 `TROSA_PI_ALLOW_WORKFILES=true` 才能启用，且不得指向运行数据、代码仓库或凭证目录。
- `TROSA_GATEWAY_URL=http://127.0.0.1:8080`、`TROSA_GATEWAY_TOKEN=`：Hamid 绑定且至少拥有 `crm:read,crm:write` 的 personal access token。只放在服务环境文件中，绝不写入 Git。

## 本地验证

```bash
TROSA_PI_AGENT_ENABLED=true \
TROSA_GATEWAY_URL=http://127.0.0.1:8080 \
TROSA_PI_MODEL=deepseek/deepseek-v4-flash \
pi --mode json --no-builtin-tools --no-context-files --no-extensions \
  -e ./pi-agent/trosa-tools.ts \
  --system-prompt "$(<./pi-agent/system-prompt.md)" \
  -p "我今天有什么要做？"
```

生产安装必须由维护者在 ECS 上安装已验证的 Pi 版本（本轮验证为 `0.84.1`）与对应 Node.js，并将上述私密变量安全写入 `/etc/trade-os/trade-os.env`；本仓库发布脚本不会上传密钥或正式数据。Pi 子进程不会继承该服务的全部环境变量，只接收所选模型的凭证、Gateway token 和上述受限路径。

## MCP Server（本地开发）

`trosa-mcp-server.mjs` 是标准 stdio MCP Server。它只读取 `TROSA_GATEWAY_URL` 与 `TROSA_GATEWAY_TOKEN`，并把九个工具请求转发给现有 `/api/gateway/*`；它不导入、打开或读取 SQLite。

先在本机安装其固定依赖：

```bash
npm install --prefix pi-agent
TROSA_GATEWAY_URL=http://127.0.0.1:8080 \
TROSA_GATEWAY_TOKEN=... \
node ./pi-agent/trosa-mcp-server.mjs
```

Pi 0.84.1 没有内置 MCP client，因此本仓库附带了仅用于验证的 `trosa-mcp-client.ts` 扩展：

```bash
pi --no-builtin-tools --no-context-files --no-extensions \
  -e ./pi-agent/trosa-mcp-client.ts -p "我今天有什么要做？"
```

该扩展经 stdio 连接上述 MCP Server，不会回退为直接 Gateway 调用。生产 ECS 不启用 Pi 或 MCP，除非维护者另行明确配置。

### Hamid 日常入口

不要在 Trosa 仓库目录直接运行通用 `pi` 来处理 CRM；它是 coding agent，默认具有本地文件和 shell 工具，既慢也会越过 Hamid 的工作边界。

使用专用启动器：

```bash
export DEEPSEEK_API_KEY=... # 或使用已配置的 Pi provider 认证
./pi-agent/trosa-hamid -p "查看我的今日待办"
```

启动器固定使用 Hamid Gateway token、Trosa stdio MCP extension 与 Hamid 系统提示；关闭内置工具、上下文文件、自动扩展和会话保存，并从临时目录运行。因此它没有 shell、`read` 或本地 SQLite / `data/` 访问能力。`TROSA_PI_MCP_ENV_FILE` 可指定替代的私有 MCP 环境文件。

Today 是确定性读取，不需要等待模型：

```bash
./pi-agent/trosa-hamid today
```

该命令直接调用同一个 MCP `get_today` 工具，一次读取 Hamid 最多 50 条到期/逾期待办；不会启动模型、Pi coding tools 或本地数据库检查。
