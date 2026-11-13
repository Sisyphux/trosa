# 代码仓库与业务数据说明

项目代码可以通过 Git 和 GitHub 协作。客户数据库、导入文件、日志、模型密钥和备份保持在独立数据目录中。

## 当前原则

- Git 管理代码、测试、设计文档和部署示例。
- `data/` 或 `CRM_DB_PATH` 管理业务数据。
- `.env` 管理本机或服务器密钥。
- 数据库和备份不提交到 Git。
- 项目目录与数据目录均避开 iCloud 等文件级实时同步位置。

## 首次连接远程仓库

在已经确认的项目根目录执行：

```bash
git status
git branch -M main
git remote add origin <仓库地址>
git push -u origin main
```

若已有 `origin`，先用 `git remote -v` 核对地址，再按需更新。保留现有 `.git` 历史，不重新初始化或删除仓库元数据。

## 日常开发

```bash
git status
git add <本次修改的文件>
git commit -m "说明本次变化"
git push
```

提交前检查：

- `.env`、`data/`、日志、虚拟环境和发布归档未进入暂存区。
- `CHANGELOG.md` 已记录用户可感知变化。
- Python、JavaScript 和相关回归测试通过。
- 文档没有把 Apple 日历订阅写成数据库同步，也没有宣称 iCloud 自动合并数据。

## 新设备

1. 克隆代码仓库。
2. 创建虚拟环境并安装 `requirements.txt`。
3. 复制 `.env.example` 为本机 `.env`，只填写所需配置。
4. 通过 `CRM_DB_PATH` 连接到被授权的唯一数据仓库，或从经过校验的备份恢复到新的独立目录。
5. 启动应用并核对 `active_store.json` 中的实际数据路径。

团队多设备日常使用推荐访问同一台已部署的 Trade OS 服务。Git 负责代码版本，应用服务负责共享同一业务数据仓库。
