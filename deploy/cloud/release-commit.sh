#!/usr/bin/env bash
# Commit 驱动的发布入口：发布不可变 commit，从不读取调用者的工作区。
#
# 用法：
#   deploy/cloud/release-commit.sh --commit <sha> [--commit <sha> ...]
#   deploy/cloud/release-commit.sh --branch <ref> [--base <ref>]
#   deploy/cloud/release-commit.sh --commit <sha> --dry-run
#   deploy/cloud/release-commit.sh --commit <sha> --allow-destructive-db
#
# 三个边界（理解这套机制只需要这三个）：
#   开发层  任务区（agent-worktree.sh）→ 分支 → commit
#   交付层  commit —— 唯一的发布输入；部署系统不认识“发布哪些文件”
#   发布层  commit → 干净 release worktree → origin/main → ECS → 健康检查
#
# 调用者的工作区（可能在途改动、脏 index、未跟踪文件）完全不参与发布：
# 不 stage、不 commit、不 push、不 checkout。所有动作都发生在一个临时 worktree
# 里，它基于 origin/<main>，只做 cherry-pick。因此“开发环境脏”与“发布候选是否
# 干净”不再互相干扰，也不需要任何人先把工作区收拾干净。
#
# 关键约束（容易漏，漏了就发布失败）：ECS 从 GitHub 公开归档按 commit SHA 下载，
# 所以 release commit 必须先出现在 GitHub 上。cherry-pick 产生的是新 SHA，因此
# 推送是发布流程自身的一步，而不是调用方的义务。
set -euo pipefail
export LC_ALL=C

# 保留原始参数：发布基础设施版本门在解析之后可能需要用它们从 origin/main 的
# 干净 worktree 重新执行同一份正式发布逻辑。
ORIGINAL_ARGS=("$@")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${TRADE_OS_SOURCE_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
SOURCE_DIR="$(git -C "$SOURCE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
TARGET_BRANCH="${TRADE_OS_AUTO_PUBLISH_BRANCH:-main}"
BASE_REF="refs/remotes/origin/$TARGET_BRANCH"
WORK_TMPDIR="${TMPDIR:-/tmp}"
# 本地发布串行化锁。放在共享 git 目录，使主工作区与所有隔离区的发布互斥；
# 可移植实现会自动回收崩溃进程留下的锁。dry-run 不改 production，不取锁。
# shellcheck source=lib-release-lock.sh
source "$SCRIPT_DIR/lib-release-lock.sh"
LOCK_DIR=""
DRY_RUN=0
ALLOW_DESTRUCTIVE="${TRADE_OS_AUTO_PUBLISH_ALLOW_DESTRUCTIVE_DB:-0}"
# Local archive is optional by default. Setting this to 1 restores the old
# (stricter) behavior where a database-sensitive release also requires a
# verified local Mac archive before publishing — opt-in only.
REQUIRE_LOCAL_BACKUP="${TRADE_OS_RELEASE_REQUIRE_LOCAL_BACKUP:-0}"
COMMIT_SPECS=()
BRANCH_SPECS=()
REV_BASE_SPEC=""
RELEASE_ID_SPEC=""
RELEASE_COMMITS=()
REL_DIR=""
MAIN_ROOT=""
ENV_FILE=""

usage() {
  cat <<'EOF'
Usage:
  deploy/cloud/release-commit.sh --commit <sha> [--commit <sha> ...]
  deploy/cloud/release-commit.sh --branch <ref> [--base <ref>]

  --commit <sha>   发布该 commit（可重复；按给出顺序 cherry-pick 到 origin/main）
  --branch <ref>   发布该分支相对 origin/main（或 --base <ref>）的全部 commit
  --base <ref>     仅用于计算 --branch 的 commit 范围；发布基线始终是 origin/main
  --release-id ID  使用指定 release id（仅影响远端 release 记录）
  --dry-run        只做 cherry-pick、数据库预检与完整回归，不推送、不发布
  --allow-destructive-db
                   显式允许疑似破坏性数据库操作（仍会先备份）
  --require-local-backup
                   数据库敏感改动额外要求本地 Mac 归档成功（默认关闭；云端
                   备份始终是发布门禁，本地归档失败不影响发布）
  --help           显示本说明

发布输入只有 commit。调用者当前的 index、未暂存改动和未跟踪文件不参与发布，
也不会被修改：流程在临时 release worktree（基于 origin/main）里执行
cherry-pick → 数据库预检 → 完整回归 → 推送 origin/main → ECS 发布 → 公网健康检查。
以已包含在 origin/main 的 commit 重复调用是幂等的（会被跳过并报告）。
EOF
}

fail() {
  printf '发布未完成：%s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit)
      [[ $# -ge 2 ]] || fail '--commit 需要一个 commit'
      COMMIT_SPECS+=("$2")
      shift 2
      ;;
    --branch)
      [[ $# -ge 2 ]] || fail '--branch 需要一个引用'
      BRANCH_SPECS+=("$2")
      shift 2
      ;;
    --base)
      [[ $# -ge 2 ]] || fail '--base 需要一个引用'
      REV_BASE_SPEC=$2
      shift 2
      ;;
    --release-id)
      [[ $# -ge 2 ]] || fail '--release-id 需要一个值'
      RELEASE_ID_SPEC=$2
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    --allow-destructive-db) ALLOW_DESTRUCTIVE=1; shift ;;
    --require-local-backup) REQUIRE_LOCAL_BACKUP=1; shift ;;
    --help|-h) usage; exit 0 ;;
    --)
      usage >&2
      fail '发布输入只能是 commit 或分支；不接受文件路径清单'
      ;;
    -*)
      usage >&2
      fail "未知参数：$1"
      ;;
    *)
      usage >&2
      fail "发布输入只能是 commit 或分支；不接受文件路径：$1"
      ;;
  esac
done

[[ -n "$SOURCE_DIR" ]] || fail '无法解析项目根目录'
[[ ${#COMMIT_SPECS[@]} -gt 0 || ${#BRANCH_SPECS[@]} -gt 0 ]] || { usage >&2; fail '必须提供 --commit 或 --branch'; }
[[ -z "$REV_BASE_SPEC" || ${#BRANCH_SPECS[@]} -gt 0 ]] \
  || fail '--base 只能和 --branch 一起使用'
if [[ -n "$RELEASE_ID_SPEC" ]] && [[ ! "$RELEASE_ID_SPEC" =~ ^[A-Za-z0-9._-]{1,128}$ ]]; then
  fail "非法 release id：$RELEASE_ID_SPEC"
fi
[[ -d "$SOURCE_DIR/.git" || -f "$SOURCE_DIR/.git" ]] || fail "$SOURCE_DIR 不是一个 Git 工作树"

# ---------------------------------------------------------------------------
# 发布基础设施版本门（release infrastructure authority）
# ---------------------------------------------------------------------------
# 同一 production commit 不应因为“从哪个 worktree 发起”而走不同版本的发布机制。
# 任务代码可以领先 main（发布输入本来就是 task HEAD/commit），但发布机制本身
# 必须来自当前权威 origin/main：否则一个历史 worktree 里残留的旧 release-commit.sh
# 会把已经废弃的发布行为（例如旧版强制本地备份）重新带回生产。
#
# 这里只检查发布基础设施（deploy/cloud/ 与 tools/release_baseline.py）是否与
# origin/main 一致，不要求任务代码等于 main。不一致时，自动从 origin/main 的干净
# 临时 worktree 重新执行同一份正式发布逻辑；解析不到 origin/main 时 fail closed。
# 逃生/自举口：TRADE_OS_RELEASE_DRIVER_MAIN=1 表示“当前已在权威驱动中”。
release_driver_is_behind() {
  local base=$1
  # HEAD 不包含 origin/main => 旧 checkout。
  git -C "$SOURCE_DIR" merge-base --is-ancestor "$base" HEAD 2>/dev/null || return 0
  # 发布基础设施相对 origin/main 有差异（无论提交还是未提交）=> 不能信任当前脚本。
  git -C "$SOURCE_DIR" diff --quiet "$base" -- deploy/cloud tools/release_baseline.py 2>/dev/null || return 0
  return 1
}

if [[ "${TRADE_OS_RELEASE_DRIVER_MAIN:-0}" != "1" ]]; then
  run_step_guard_label="发布基础设施版本检查"
  printf '\n==> %s（要求来自最新 origin/%s）\n' "$run_step_guard_label" "$TARGET_BRANCH"
  git -C "$SOURCE_DIR" fetch --quiet origin "$TARGET_BRANCH" \
    || fail "无法同步 origin/$TARGET_BRANCH，不能确认发布脚本是否为最新；拒绝用旧脚本发布（fail closed）"
  DRIVER_BASE="$(git -C "$SOURCE_DIR" rev-parse --verify --quiet "$BASE_REF" || true)"
  [[ -n "$DRIVER_BASE" ]] || fail "无法读取 origin/$TARGET_BRANCH"
  if release_driver_is_behind "$DRIVER_BASE"; then
    DRIVER_DIR="$(mktemp -d "${WORK_TMPDIR}/trosa-release-driver.XXXXXX")" \
      || fail '无法创建发布驱动临时目录'
    git -C "$SOURCE_DIR" worktree add --detach --quiet "$DRIVER_DIR" "$DRIVER_BASE" \
      || fail "无法从 origin/$TARGET_BRANCH 创建干净发布驱动 worktree"
    printf '当前脚本不是最新 origin/%s；改从权威 release worktree 重新执行发布：%s\n' \
      "$TARGET_BRANCH" "$DRIVER_DIR"
    driver_status=0
    env -u TRADE_OS_SOURCE_DIR \
      TRADE_OS_RELEASE_DRIVER_MAIN=1 \
      bash "$DRIVER_DIR/deploy/cloud/release-commit.sh" \
      ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"} || driver_status=$?
    git -C "$SOURCE_DIR" worktree remove --force -- "$DRIVER_DIR" >/dev/null 2>&1 \
      || rm -rf -- "$DRIVER_DIR"
    git -C "$SOURCE_DIR" worktree prune >/dev/null 2>&1 || true
    exit "$driver_status"
  fi
  printf '完成：%s（当前脚本已与 origin/%s 的发布机制一致）\n' \
    "$run_step_guard_label" "$TARGET_BRANCH"
fi

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$REL_DIR" && -d "$REL_DIR" ]]; then
    cd "$SOURCE_DIR" 2>/dev/null || true
    git -C "$SOURCE_DIR" worktree remove --force -- "$REL_DIR" >/dev/null 2>&1 \
      || rm -rf -- "$REL_DIR"
  fi
  if [[ -n "$REL_DIR" ]]; then
    git -C "$SOURCE_DIR" worktree prune >/dev/null 2>&1 || true
  fi
  if [[ -n "$LOCK_DIR" ]]; then
    trosa_lock_release "$LOCK_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT

run_step() {
  local label=$1
  shift
  printf '\n==> %s\n' "$label"
  if "$@"; then
    printf '完成：%s\n' "$label"
  else
    local status=$?
    printf '失败：%s（退出码 %s）\n' "$label" "$status" >&2
    exit "$status"
  fi
}

# 主仓根目录：任务区与 release worktree 都通过 git common dir 找到它，用于复用
# .venv / node_modules，并定位只存在于主仓的发布路由文件。
GIT_COMMON_DIR="$(git -C "$SOURCE_DIR" rev-parse --git-common-dir 2>/dev/null || true)"
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$SOURCE_DIR/$GIT_COMMON_DIR" ;;
esac
MAIN_ROOT="$(cd "$GIT_COMMON_DIR/.." 2>/dev/null && pwd || true)"
[[ -n "$MAIN_ROOT" ]] || MAIN_ROOT="$SOURCE_DIR"

# shellcheck source=release-env.sh
source "$SCRIPT_DIR/release-env.sh"

# 发布角色边界：dry-run 只构建候选 + 跑门禁，不改 production，任何角色可用；
# 真正发布会推送 origin/main 并切换 ECS，必须由 release 角色执行。
if [[ "$DRY_RUN" != 1 ]]; then
  trosa_require_release_role || exit 1
fi

# 正式位置在仓库外（~/.config/trosa/workbench.env）：开发 Agent 的 worktree
# 不再天然携带发布配置。TRADE_OS_WORKBENCH_ENV 仍可显式覆盖；旧仓库内位置保留
# 兼容并提示迁移。
ENV_FILE="$(trosa_resolve_workbench_env "$SCRIPT_DIR" "$MAIN_ROOT")"
if [[ "$ENV_FILE" != /* ]]; then
  ENV_DIR="$(cd "$(dirname "$ENV_FILE")" 2>/dev/null && pwd || true)"
  [[ -n "$ENV_DIR" ]] || fail "发布配置路径无效：$ENV_FILE"
  ENV_FILE="$ENV_DIR/$(basename "$ENV_FILE")"
fi
if [[ ! -r "$ENV_FILE" ]]; then
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '%s\n' "提示：dry-run 未找到发布配置 $ENV_FILE；本次不会访问 ECS。" >&2
    ENV_FILE=""
  else
    fail "找不到发布配置 $ENV_FILE；请把 workbench.env.example 复制到 $(trosa_config_home)/workbench.env，或用 TRADE_OS_WORKBENCH_ENV 指向它"
  fi
fi
trosa_warn_legacy_workbench_env "$ENV_FILE"

if [[ "$DRY_RUN" != 1 ]]; then
  LOCK_DIR="$GIT_COMMON_DIR/trosa-release.lock"
  trosa_lock_acquire "$LOCK_DIR" "${TRADE_OS_RELEASE_LOCK_WAIT:-1800}" 7200 \
    || fail "已有另一个本地发布正在运行（锁：$LOCK_DIR，等待 ${TRADE_OS_RELEASE_LOCK_WAIT:-1800}s 超时）；确认无进程后重试，或删除该目录"
fi

cd "$SOURCE_DIR"
origin_url="$(git remote get-url origin 2>/dev/null || true)"
[[ -n "$origin_url" ]] || fail '找不到 origin 远程仓库'

run_step "同步远端 $TARGET_BRANCH 基线" git fetch --quiet origin "$TARGET_BRANCH"
BASE_SHA="$(git rev-parse --verify --quiet "$BASE_REF" || true)"
[[ -n "$BASE_SHA" ]] || fail "无法读取 origin/$TARGET_BRANCH"
printf '\n发布基线 origin/%s = %s\n' "$TARGET_BRANCH" "${BASE_SHA:0:9}"

add_commit() {
  local sha=$1 existing
  if git merge-base --is-ancestor "$sha" "$BASE_SHA"; then
    printf '跳过 %s：已包含在 origin/%s 中\n' "${sha:0:9}" "$TARGET_BRANCH"
    return 0
  fi
  if [[ ${#RELEASE_COMMITS[@]} -gt 0 ]]; then
    for existing in "${RELEASE_COMMITS[@]}"; do
      if [[ "$existing" == "$sha" ]]; then
        printf '跳过 %s：本次已列出\n' "${sha:0:9}"
        return 0
      fi
    done
  fi
  RELEASE_COMMITS+=("$sha")
}

# 任务毕业门：发布只接受真正 ready 的任务。任务分支必须携带对应的完成证据
# （门禁结果对应该分支 tip），且该 tip 已包含最新 origin/main。这里与
# agent-worktree.sh publish 使用同一判定，防止绕过 publish 直接 --branch。
enforce_agent_branch_ready() {
  local spec=$1 branch_sha=$2 task meta status verify verified
  [[ "$spec" == agent/* ]] || return 0
  [[ "$DRY_RUN" == 1 ]] && return 0
  task="${spec#agent/}"
  meta="$GIT_COMMON_DIR/trosa-tasks/$task.json"
  [[ -r "$meta" ]] \
    || fail "发布被拒绝：任务 $task 缺少完成证据清单 $meta；请先 test --task $task 生成证据"
  read -r status verify verified < <(python3 - "$meta" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = json.load(handle)
print(doc.get("status") or "active", doc.get("verify_result") or "", doc.get("verified_commit") or "")
PY
)
  [[ "$status" != "abandoned" ]] || fail "发布被拒绝：任务 $task 已废弃"
  [[ "$verify" == "ok" ]] \
    || fail "发布被拒绝：任务 $task 没有有效门禁证据（verify_result=${verify:-无}）；先 test --task $task"
  [[ "$verified" == "$branch_sha" ]] \
    || fail "发布被拒绝：任务 $task 的证据对应 commit ${verified:0:9}，与分支 tip ${branch_sha:0:9} 不一致；重新 test --task $task"
  git merge-base --is-ancestor "$BASE_SHA" "$branch_sha" \
    || fail "发布被拒绝：任务 $task 未基于最新 origin/$TARGET_BRANCH；先 sync --task $task 并重新 test"
}

if [[ ${#COMMIT_SPECS[@]} -gt 0 ]]; then
  for spec in "${COMMIT_SPECS[@]}"; do
    resolved="$(git rev-parse --verify --quiet --end-of-options "${spec}^{commit}" || true)"
    [[ -n "$resolved" ]] || fail "本地仓库找不到 commit：$spec（先在任务区完成 commit）"
    add_commit "$resolved"
  done
fi

if [[ ${#BRANCH_SPECS[@]} -gt 0 ]]; then
  rev_base="$BASE_SHA"
  if [[ -n "$REV_BASE_SPEC" ]]; then
    rev_base="$(git rev-parse --verify --quiet --end-of-options "${REV_BASE_SPEC}^{commit}" || true)"
    [[ -n "$rev_base" ]] || fail "--base 引用不存在：$REV_BASE_SPEC"
  fi
  for spec in "${BRANCH_SPECS[@]}"; do
    branch_sha="$(git rev-parse --verify --quiet --end-of-options "${spec}^{commit}" || true)"
    [[ -n "$branch_sha" ]] || fail "找不到分支或引用：$spec"
    enforce_agent_branch_ready "$spec" "$branch_sha"
    branch_list="$(git rev-list --reverse "$rev_base".."$branch_sha")"
    if [[ -z "$branch_list" ]]; then
      printf '分支 %s 相对 %s 没有新 commit\n' "$spec" "${rev_base:0:9}"
    fi
    while IFS= read -r item; do
      [[ -n "$item" ]] || continue
      add_commit "$item"
    done <<< "$branch_list"
  done
fi

if [[ ${#RELEASE_COMMITS[@]} -eq 0 ]]; then
  printf '\nRELEASE_COMMIT_ALREADY_PRESENT base=%s commits=0\n' "$BASE_SHA"
  printf '%s\n' "所有输入 commit 已经包含在 origin/$TARGET_BRANCH；无需创建 release worktree、推送或发布。"
  exit 0
fi

REL_DIR="$(mktemp -d "$WORK_TMPDIR/trosa-release.XXXXXX")" || fail '无法创建临时 release 目录'
run_step '创建干净 release worktree' git worktree add --detach --quiet "$REL_DIR" "$BASE_SHA"
printf 'release worktree：%s（不接触调用者工作区）\n' "$REL_DIR"

[[ -x "$MAIN_ROOT/.venv/bin/python" ]] \
  || fail "主仓 .venv 不可用（$MAIN_ROOT/.venv）；先按 requirements.txt 准备依赖"
ln -s -- "$MAIN_ROOT/.venv" "$REL_DIR/.venv"
[[ -d "$MAIN_ROOT/browser-extension/node_modules" ]] \
  || fail '主仓 browser-extension/node_modules 缺失；先在 browser-extension 执行 npm install'
ln -s -- "$MAIN_ROOT/browser-extension/node_modules" "$REL_DIR/browser-extension/node_modules"

printf '\n==> 应用 %s 个 commit 到 %s\n' "${#RELEASE_COMMITS[@]}" "${BASE_SHA:0:9}"
cd "$REL_DIR"
for sha in "${RELEASE_COMMITS[@]}"; do
  printf '  cherry-pick %s\n' "${sha:0:9}"
  if git cherry-pick -x "$sha" >/dev/null 2>&1; then
    continue
  fi
  conflicts="$(git diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ' || true)"
  git cherry-pick --abort >/dev/null 2>&1 || true
  printf '冲突文件：%s\n' "${conflicts:-（无法读取，见 git status）}" >&2
  fail "cherry-pick ${sha:0:9} 与 origin/$TARGET_BRANCH 冲突，已放弃；production 与 origin/$TARGET_BRANCH 均未变更。请先在该任务区 sync 到最新 origin/$TARGET_BRANCH 后重跑"
done
RELEASE_SHA="$(git rev-parse HEAD)"
printf 'release commit = %s\n' "$RELEASE_SHA"

# A release commit is the only publish input. Keep operational data, local
# credentials, and generated runtimes out even when they were accidentally
# committed on an Agent branch.
while IFS= read -r path; do
  [[ -n "$path" ]] || continue
  [[ "$path" == ".env.example" ]] && continue
  case "$path" in
    data|data/*|app/data|app/data/*|*.db|*.db-*|*.sqlite|*.sqlite-*|*.sqlite3|*.bak|*.wal|*.shm|*.log|.env|.env.*|deploy/cloud/workbench.env|deploy/macos/cloudflared.yml|.venv|.venv/*|node_modules|*/node_modules/*)
      fail "发布 commit 包含禁止路径：$path"
      ;;
  esac
done <<< "$(git diff --name-only "$BASE_SHA" "$RELEASE_SHA")"

# ---- 数据库预检：与 tools/release_db_plan.py 同一分类器 ------------------------
CHANGED_FILES="$(git diff --name-only "$BASE_SHA" "$RELEASE_SHA")"
DB_SENSITIVE=0
DB_FILES=()
while IFS= read -r path; do
  [[ -n "$path" ]] || continue
  case "$path" in
    migrations/*.sql|db.py|postgres_compat.py|postgres_schema_contract.py|tools/unified_postgres_migration.py|tools/unified_postgres_import.py|deploy/postgres-production/*)
      DB_SENSITIVE=1
      DB_FILES+=("$path")
      ;;
  esac
done <<< "$CHANGED_FILES"

if [[ "$DB_SENSITIVE" == 1 ]]; then
  printf '\n==> 数据库敏感改动：\n'
  while IFS= read -r db_path; do
    if [[ -n "$db_path" ]]; then printf '  %s\n' "$db_path"; fi
  done <<< "$(printf '%s\n' ${DB_FILES[@]+"${DB_FILES[@]}"})"
  DESTRUCTIVE_HIT=""
  DB_SQL_FILES=()
  # 破坏性启发式只判可执行迁移/运行时文件。migrations/README.md 这类文档会
  # 描述 DROP/DELETE 关键字，但不在迁移时执行，不能因此阻塞发布。
  DB_DIFF_FILES=()
  while IFS= read -r path; do
    case "$path" in
      migrations/*.sql) DB_SQL_FILES+=("$path"); DB_DIFF_FILES+=("$path") ;;
      *.py) DB_DIFF_FILES+=("$path") ;;
    esac
  done <<< "$CHANGED_FILES"
  if [[ ${#DB_SQL_FILES[@]} -gt 0 ]]; then
    # 分类器返回非 0 时输出破坏性文件清单；返回 0 表示全部兼容。
    if DESTRUCTIVE_HIT="$("$REL_DIR/.venv/bin/python" "$REL_DIR/tools/release_db_plan.py" \
        --check-files "$REL_DIR" ${DB_SQL_FILES[@]+"${DB_SQL_FILES[@]}"} 2>/dev/null)"; then
      DESTRUCTIVE_HIT=""
    fi
  fi
  if [[ -z "$DESTRUCTIVE_HIT" && ${#DB_DIFF_FILES[@]} -gt 0 ]]; then
    # 与 auto-publish.sh 一致的回退启发式，覆盖无新迁移但改了运行时迁移代码的情况。
    DESTRUCTIVE_GREP='^\+[^+].*(DROP[[:space:]]+(TABLE|TABLES|COLUMN|SCHEMA|DATABASE)|TRUNCATE[[:space:]]+(TABLE|TABLES)|DELETE[[:space:]]+FROM|ALTER[[:space:]]+TABLE.*DROP[[:space:]]+COLUMN)'
    DB_DIFF="$(git diff "$BASE_SHA" "$RELEASE_SHA" -- "${DB_DIFF_FILES[@]}")"
    if printf '%s\n' "$DB_DIFF" | grep -Eiq "$DESTRUCTIVE_GREP"; then
      DESTRUCTIVE_HIT="(grep fallback on changed runtime files)"
    fi
  fi
  if [[ -n "$DESTRUCTIVE_HIT" ]]; then
    printf '疑似破坏性数据库操作：\n%s\n' "$DESTRUCTIVE_HIT" >&2
    if [[ "$ALLOW_DESTRUCTIVE" != 1 ]]; then
      fail '检测到疑似破坏性数据库操作；确认后用 --allow-destructive-db（或 TRADE_OS_AUTO_PUBLISH_ALLOW_DESTRUCTIVE_DB=1）重跑'
    fi
    printf '警告：已显式允许疑似破坏性数据库操作；发布前仍会先备份。\n' >&2
  fi
fi

# 本地不做迁移账本判断：正式账本只在 ECS 的 PostgreSQL 里，缺账本会把全部迁移
# 都当成待应用（从而虚报 destructive）。权威 db-plan 由 ECS runner 在切换流量前
# 基于真实账本完成；本地只报告本次 release 新增的迁移文件。
NEW_MIGRATIONS="$(git diff --name-only --diff-filter=A "$BASE_SHA" "$RELEASE_SHA" -- migrations/)"
if [[ -n "$NEW_MIGRATIONS" ]]; then
  printf '\n==> 本次 release 新增迁移（ECS 会在切换流量前应用，并先做服务端备份）：\n'
  while IFS= read -r migration; do
    if [[ -n "$migration" ]]; then printf '  %s\n' "$migration"; fi
  done <<< "$NEW_MIGRATIONS"
else
  printf '\n==> 本次 release 不新增迁移文件（ECS 端 db-plan 仍会核对正式账本）。\n'
fi

# 门禁定义来自正在运行的入口本身，而不是被测的候选代码：候选不可自证合格。
[[ -r "$SCRIPT_DIR/release-test.sh" ]] || fail "找不到发布门禁 $SCRIPT_DIR/release-test.sh"
run_step '完整本地回归（干净 release worktree）' bash "$SCRIPT_DIR/release-test.sh" --dir "$REL_DIR"

check_cloud_status() {
  local output
  if ! output="$(TRADE_OS_WORKBENCH_ENV="$ENV_FILE" bash "$SCRIPT_DIR/status-workbench.sh" 2>&1)"; then
    printf '%s\n' "$output" >&2
    return 1
  fi
  printf '%s\n' "$output"
  if ! printf '%s\n' "$output" | grep -Eq 'TROSA_MANAGER_STATUS .*app=active .*tunnel=active .*health=ok'; then
    printf '发布前 ECS 未同时满足 app=active、tunnel=active、health=ok。\n' >&2
    return 1
  fi
}
if [[ "$DRY_RUN" == 1 ]]; then
  printf '\nRELEASE_DRY_RUN_OK base=%s release_commit=%s commits=%s\n' \
    "$BASE_SHA" "$RELEASE_SHA" "${#RELEASE_COMMITS[@]}"
  printf '%s\n' '未推送、未发布，production 未变更；release worktree 将被回收。'
  exit 0
fi

run_step '发布前 ECS 状态' check_cloud_status

# 数据库敏感改动的安全要求不变：发布前必须有可靠、已校验、可恢复的备份。
# 但这份权威备份在 ECS 上生成并保留（release-remote.sh 的 backup 阶段，
# 校验大小/SHA-256/pg_restore --list，失败会中止发布），不再要求先下载到
# 本地 Mac。workbench download / scp / SSH 文件流失败不会阻塞发布。
if [[ "$DB_SENSITIVE" == 1 ]]; then
  printf '\n==> 数据库敏感改动：权威备份在 ECS 切换流量前生成并校验（失败会中止发布）\n'
  printf '    本地 Mac 归档为可选，不再是发布前置条件（如需强制：--require-local-backup）。\n'
  if [[ "$REQUIRE_LOCAL_BACKUP" == 1 ]]; then
    run_step '数据库敏感改动本地归档（显式要求）' \
      env TRADE_OS_WORKBENCH_ENV="$ENV_FILE" TRADE_OS_BACKUP_TRANSFER="${TRADE_OS_BACKUP_TRANSFER:-auto}" \
      bash "$SCRIPT_DIR/backup-workbench.sh" --download=require
  fi
fi

# 推送是必须的一步：ECS 按 commit SHA 从 GitHub 公开归档下载，本地存在不等于
# 远端可达。--force-with-lease 固定期望的远端值，防止覆盖并发发布。
run_step "推送 release commit 到 GitHub $TARGET_BRANCH" \
  git push --quiet origin "HEAD:refs/heads/$TARGET_BRANCH" \
  "--force-with-lease=refs/heads/$TARGET_BRANCH:$BASE_SHA"
remote_after="$(git ls-remote origin "refs/heads/$TARGET_BRANCH" | awk 'NR == 1 {print $1}')"
[[ "$remote_after" == "$RELEASE_SHA" ]] \
  || fail "GitHub $TARGET_BRANCH 未确认到本次 release commit：期望 $RELEASE_SHA，实际 ${remote_after:-（空）}"

RELEASE_ID="${RELEASE_ID_SPEC:-${TRADE_OS_RELEASE_ID:-rel-$(date -u +%Y%m%d%H%M%S)-${RELEASE_SHA:0:12}}}"
[[ "$RELEASE_ID" =~ ^[A-Za-z0-9._-]{1,128}$ ]] \
  || fail "非法 release id：$RELEASE_ID"
publish_args=(publish --commit "$RELEASE_SHA" --release-id "$RELEASE_ID")
if [[ "$ALLOW_DESTRUCTIVE" == 1 ]]; then
  publish_args+=(--allow-destructive-db)
fi

publish_output=""
if publish_output="$(TRADE_OS_WORKBENCH_ENV="$ENV_FILE" TRADE_OS_SOURCE_DIR="$SOURCE_DIR" \
    bash "$SCRIPT_DIR/trosa-release" ${publish_args[@]+"${publish_args[@]}"} 2>&1)"; then
  printf '%s\n' "$publish_output"
else
  publish_status=$?
  printf '%s\n' "$publish_output" >&2
  printf 'GitHub 已保存 release commit=%s，但 ECS 未确认上线；服务端 runner 已保留上一健康 release，重跑同一命令是安全的。\n' "$RELEASE_SHA" >&2
  exit "$publish_status"
fi

# workbench.env contains routing metadata only. Source it once here so quoted
# URLs and an optional `export` form are handled exactly as by trosa-release.
source "$ENV_FILE"
PUBLIC_URL="${TRADE_OS_PUBLIC_URL:-https://app.trosa.space}"
case "$PUBLIC_URL" in
  http://*|https://*) ;;
  *) fail "无效的 TRADE_OS_PUBLIC_URL：$PUBLIC_URL" ;;
esac
PUBLIC_HEALTH_URL="${PUBLIC_URL%/}/api/network/ping"

# 与 auto-publish.sh 一致：ECS 本机深度健康已通过时，公网抖动不触发自动回滚。
public_ok=0
for attempt in 1 2 3 4 5 6; do
  public_body="$(curl --fail --silent --show-error --max-time 10 "$PUBLIC_HEALTH_URL" 2>/dev/null || true)"
  if printf '%s' "$public_body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    public_ok=1
    break
  fi
  sleep 2
done

if [[ "$public_ok" != 1 ]]; then
  printf 'ECS 本机发布健康检查已通过，但公网健康接口暂未确认；未因可能的 Tunnel/网络抖动自动回滚。\n' >&2
  printf 'commit=%s release=%s public_health=unknown url=%s\n' "$RELEASE_SHA" "$RELEASE_ID" "$PUBLIC_HEALTH_URL" >&2
  exit 1
fi

# 可选本地归档：发布已经成功，云端备份也已校验。这里再拉一份 Mac 归档只是
# 便利；workbench/scp/SSH 文件流失败一律不改变发布结果。
LOCAL_ARCHIVE_STATUS="skipped"
if [[ "$DB_SENSITIVE" == 1 && "$REQUIRE_LOCAL_BACKUP" != 1 ]]; then
  printf '\n==> 可选本地归档（失败不影响发布结果）\n'
  if env TRADE_OS_WORKBENCH_ENV="$ENV_FILE" TRADE_OS_BACKUP_TRANSFER="${TRADE_OS_BACKUP_TRANSFER:-auto}" \
      bash "$SCRIPT_DIR/backup-workbench.sh" --download=auto; then
    LOCAL_ARCHIVE_STATUS="ok"
  else
    archive_rc=$?
    printf 'TROSA_RELEASE_LOCAL_ARCHIVE local_download_failed rc=%s（云端备份已完成，发布保持成功）\n' "$archive_rc" >&2
    LOCAL_ARCHIVE_STATUS="local_download_failed"
  fi
fi

printf '\nRELEASE_COMMIT_SUCCESS commit=%s base=%s release=%s commits=%s public_health=ok local_archive=%s\n' \
  "$RELEASE_SHA" "${BASE_SHA:0:9}" "$RELEASE_ID" "${#RELEASE_COMMITS[@]}" "$LOCAL_ARCHIVE_STATUS"
printf '本地 %s 分支未移动（调用者工作区保持原样）；需要跟随时执行 git fetch origin && git merge --ff-only origin/%s\n' \
  "$TARGET_BRANCH" "$TARGET_BRANCH"
