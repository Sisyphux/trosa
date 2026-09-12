// Trosa 外贸 CRM — 当前状态 C4 模型
// 场景技能：system-modeler ｜ 基础技能：c4model
//
// 视图：L1 系统上下文、L2 容器、L3 组件（Flask 应用进程内部）
// 证据基线：2026-09-12 16:28 CST 发布后运行态（commit 0a25827，release auto-20260912082853-0a25827；包含功能收敛 commit 7535b79），
//           指纹与远端核验见 trosa-system.summary.md / trosa-system.evidence.md
// 证据索引与逐条置信度：trosa-system.evidence.md
//
// 已知 UNKNOWN（图中按原样保留，未画成 confirmed 路径）：
//   1. Cloudflare Access 策略未在仓库中体现，仓库只体现 Tunnel（deploy/cloudflared.service）
//   2. 客户"阶段"与"判词"的取值未核实（等级词汇已查明，见证据文件）
//
// 打开方式：在 Qoder 中用 Structurizr DSL 格式查看器打开本文件

workspace "Trosa 外贸 CRM" "三人使用的外贸 CRM 工作台。当前状态架构模型：系统上下文、容器与组件。" {

    model {

        // ============================================================
        // L1 人员
        // ============================================================

        salesUser = person "销售用户" "Trosa 团队 3 人（用户注册表 db.py:29-34）。在客户工作区恢复上下文、记录沟通、执行已确认动作、确认下一步及日期。"
        operator = person "开发兼运维（同一团队）" "通过本地发布脚本与云主机 Workbench 完成发布、备份与回滚。"

        // ============================================================
        // L1 外部系统
        // ============================================================

        cloudflare = softwareSystem "Cloudflare 边缘与 Tunnel" "公网入口 app.trosa.space 回源到本机 127.0.0.1:8080。仓库只体现 Tunnel（deploy/cloudflared.service, deploy/cloudflared-config.yml.example）；Access 策略未在仓库中体现（UNKNOWN）。" "External"

        sela = softwareSystem "Sela 外联代理" "同步已确认外联。经受限接口 /api/integrations/sela/*（16 条路由）接入，使用精确身份匹配、幂等键，歧义进 REVIEW。其本地 runtime、HTTPS Gateway、有界 outbox 和 Gmail delivery journal 只承担 Agent 执行与可靠性，不保存 Trosa 业务副本。" "External"

        gmailApi = softwareSystem "Google Gmail API" "只读范围 gmail.readonly。OAuth 授权后增量同步邮件，规范化后写入沟通时间线与 Inbox。" "External"

        llmProviders = softwareSystem "AI 模型服务" "DeepSeek / 通义千问 / 智谱 / OpenAI / LM Studio / Ollama。未配置 API Key 时核心功能完整可用，仅 AI 能力不可用。" "External"

        recipientMta = softwareSystem "收件方邮件服务器" "SMTP 可达性探测：MX 查询 + 握手，不发送 DATA。默认关闭。" "External"

        appleCalendar = softwareSystem "Apple 日历" "仅订阅 /api/calendar/ical/<token>.ics，单向只读，不能回写 CRM。" "External"

        // ============================================================
        // 系统边界
        // ============================================================

        trosa = softwareSystem "Trosa 外贸 CRM" "核心闭环：恢复客户上下文 → 执行已确认动作 → 记录实际事实 → 按需确认下一步及日期。沟通记录可以没有下一步；待办必须同时有明确动作和日期。" {

            // ---------------- L2 容器 ----------------

            webUi = container "Web 工作台前端" "客户工作区、Today、Inbox、Search、日历与周报视图。由 Flask 以静态资源直接提供（app.py:126-133，static_url_path=''）。" "HTML + 原生 JavaScript（app/static/app.js，8,480 行）"

            appServer = container "Flask 应用进程" "单进程、单模块、158 条路由、无蓝图（register_blueprint / add_url_rule 实测计数为 0）。生产由 serve.py 用 Waitress 启动，并以 importlib 按 'trade_os_web' 加载 app.py，以避开 app/ 包名冲突；后台调度以线程方式运行在同一进程内。" "Python 3 + Flask + Waitress" {

                // ------------ L3 组件（模块级；证据 = 真实 import 边，无环）------------

                routeLayer = component "HTTP 路由与会话层（app.py）" "158 条路由（31 个 /api 前缀：154 条 API + 4 条非 API）集中在单个约 1.5 万行模块内：客户 36、integrations 21（Sela 16 + Gmail 5）、gateway 11、reminders 9、Inbox 8、agent 8、auth 5、team 5 等。功能分组是分析性划分——代码中不存在模块级边界。" "Python"

                domainCore = component "Trosa 业务域（trosa_domain.py）" "业务读写模型与客户事实投影：客户/联系人、沟通时间线、任务合并（每个到期日一个任务）、Inbox 事实、外联投递历史。1,294 行。" "Python"

                dataAccess = component "数据访问与用户上下文（db.py）" "用户注册表、后端选择（postgres_mode 门控）、连接、完整性检查、备份/恢复、启动维护、迁移应用与账本。2,076 行。" "Python"

                pgCompat = component "SQLite→PostgreSQL 兼容层（postgres_compat.py）" "把 SQLite 形状的 SQL（? 占位符、sqlite3.Row）改写并路由到 PostgreSQL。317 行。" "Python"

                schemaContract = component "架构契约（postgres_schema_contract.py）" "声明式必需 schema / 表 / 视图 / 列 / 索引 / 函数 / 触发器；在启动与迁移前后校验。310 行。" "Python"

                aiEngine = component "AI 与抓取引擎（app/engine.py）" "多模型供应商适配、图片 OCR、网页抓取、搜索。核心路径不依赖此组件。1,550 行。" "Python"

                gmailSync = component "Gmail 同步（gmail_sync.py）" "OAuth 授权、刷新令牌加密、增量同步、消息规范化、联系人匹配。只读，不自动创建客户或联系人。1,263 行。" "Python"

                mailVerifier = component "邮箱验证（email_verifier.py）" "SMTP 可达性探测 worker，不发送 DATA。297 行。" "Python"

                backgroundJobs = component "后台调度（scheduler.py）" "APScheduler（Asia/Shanghai）：每日本地快照 02:15、邮箱验证 worker、Gmail 同步 worker。161 行。" "Python"

                icsGen = component "ICS 生成（ical_gen.py）" "RFC 5545 日历源生成，供 Apple 日历订阅。143 行。" "Python"

                settings = component "配置（config.py）" "存储保留策略与邮箱验证开关。45 行。" "Python"
            }

            postgres = container "PostgreSQL（业务数据单一事实源）" "tradeos 库，127.0.0.1:5432。6 个 schema；契约声明 63 个必需关系与 38 个必需视图；由 migrations/0001–0029 维护。SQLite 仅用于隔离开发、导入导出与经批准的历史恢复边界。" "PostgreSQL" "Database"

            evidenceStore = container "附件与回滚材料存储" "云主机本机 /var/lib/trade-os：上传附件、导入来源、历史回滚材料。" "文件系统" "Database"

            captureExtension = container "沟通采集浏览器扩展" "MV3 侧边栏扩展 v0.5.0，从网页邮箱与 WhatsApp Web 采集沟通并提交。宿主权限限定在 163/126/yeah/qiye 邮箱、web.whatsapp.com 与 app.trosa.space。" "Chrome MV3 + JavaScript"
        }

        // ============================================================
        // 关系（每条均对应 evidence 文件中的证据条目）
        // ============================================================

        salesUser -> webUi "使用工作台" "HTTPS"
        salesUser -> captureExtension "在网页邮箱 / WhatsApp 中采集沟通"
        operator -> trosa "发布、备份与回滚" "git push + Workbench / SSH"

        salesUser -> cloudflare "访问 app.trosa.space" "HTTPS"
        cloudflare -> appServer "隧道回源 127.0.0.1:8080" "HTTP"

        appServer -> webUi "提供静态资源"
        webUi -> appServer "调用 /api/*" "JSON over HTTPS"

        captureExtension -> appServer "提交采集到的沟通" "JSON over HTTPS（含 /api/extension/match、/api/extension/communications）"

        appServer -> postgres "读写业务数据" "SQL（经 postgres_compat 或 psycopg）"
        appServer -> evidenceStore "读写附件与回滚材料" "文件"
        appServer -> icsGen "生成 ICS 订阅源" "in-process"

        sela -> appServer "同步已确认外联" "HTTPS + Bearer（/api/integrations/sela/*）"
        appServer -> gmailApi "增量读取邮件（只读）" "HTTPS / OAuth"
        appServer -> llmProviders "可选：AI 能力" "HTTPS"
        appServer -> recipientMta "可选：SMTP 可达性探测" "SMTP（不发送 DATA）"
        appleCalendar -> appServer "订阅 ICS 日历源" "HTTPS GET"

        // 组件级关系（来自真实 import 边）
        routeLayer -> domainCore "调用业务读写模型"
        routeLayer -> dataAccess "读写连接与用户上下文"
        routeLayer -> aiEngine "可选 AI 能力"
        routeLayer -> gmailSync "触发/查询同步"
        routeLayer -> backgroundJobs "读取/控制调度状态"
        routeLayer -> settings "读取配置"
        routeLayer -> pgCompat "PostgreSQL 模式下取连接"
        domainCore -> dataAccess "读取后端模式"
        gmailSync -> domainCore "写入沟通事实"
        gmailSync -> aiEngine "可选摘要"
        mailVerifier -> dataAccess "读取待处理任务"
        backgroundJobs -> dataAccess "调度状态"
        dataAccess -> schemaContract "校验必需关系"
        dataAccess -> pgCompat "PostgreSQL 连接"
    }

    views {

        systemContext trosa "L1SystemContext" {
            title "L1 系统上下文 — Trosa 外贸 CRM"
            include *
            autoLayout tb
        }

        container trosa "L2Containers" {
            title "L2 容器 — Trosa 外贸 CRM"
            include *
            autoLayout tb
        }

        component appServer "L3Components" {
            title "L3 组件 — Flask 应用进程内部"
            include *
            autoLayout tb
        }

        styles {
            element "Person" {
                shape person
                background "#08427b"
                color "#ffffff"
            }
            element "Software System" {
                background "#1168bd"
                color "#ffffff"
            }
            element "External" {
                background "#8a8a8a"
                color "#ffffff"
            }
            element "Container" {
                background "#438dd5"
                color "#ffffff"
            }
            element "Database" {
                shape cylinder
                background "#2f6f4f"
                color "#ffffff"
            }
            element "Component" {
                background "#85bbf0"
                color "#000000"
            }
        }
    }
}
