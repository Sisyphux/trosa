# migrations/ 目录约定

- 文件名：`NNNN_lower_snake_case.sql`，4 位序号从 `0001` 开始，**唯一**。并行任务可能
  先发布较大编号，因此编号出现空档只作为警告，不是错误；空档不会让运行时漏掉任何迁移。
- **目录是唯一事实源**：`db.py` 与 `tools/unified_postgres_migration.py` 都按文件名排序
  自动发现迁移，不存在需要同步维护的第二份清单。新增迁移只需放入文件。
- 新迁移编号必须先预留：`deploy/cloud/agent-worktree.sh create/adopt` 会把下一个空号写入
  任务清单的 `reserved_migration`（跨主工作区与所有隔离区计算）。两个并行任务抢同一个
  编号时，后合并者在合并前 `git mv` 到下一个空号。
- **自动消解碰撞**：`sync`/`publish` 会自动调用 `tools/reconcile_migrations.py`，把本任务
  新增且与最新 main、其它 worktree 或他人预留冲突的迁移改名到下一个空号并提交；无冲突
  时不动任何文件。也可单独运行 `deploy/cloud/agent-worktree.sh reconcile --task <id>`。
- 校验命令：

  ```bash
  python3 tools/check_migrations.py --dir .
  ```

  它检查文件名合法、编号唯一（空档仅警告）；已接入 `deploy/cloud/release-test.sh` 的快速门禁
  和 `tests/test_migration_integrity.py`。
- 迁移是 **forward-only**：已应用的迁移文件禁止再改内容（运行时按 `audit.schema_migrations`
  里的 SHA-256 校验并拒绝重放/篡改），任何修正都必须新增前向迁移。
- 破坏性 SQL（迁移时执行的 `DROP TABLE/COLUMN`、`TRUNCATE`、`DELETE FROM`、
  `ALTER TABLE ... DROP COLUMN`）会阻塞自动发布，需要明确 `--allow-destructive-db` 并先备份；
  触发器/函数体内的行同步 `DELETE` 与 `DROP INDEX/CONSTRAINT` 不算破坏性。
