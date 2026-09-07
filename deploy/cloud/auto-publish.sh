#!/usr/bin/env bash
# Verify, commit, push, publish, and smoke-test one completed Trosa change.
#
# File paths are intentionally explicit. This command must never turn an
# unrelated worktree change into a production release by using `git add .`.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${TRADE_OS_SOURCE_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
SOURCE_DIR="$(git -C "$SOURCE_DIR" rev-parse --show-toplevel 2>/dev/null)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
if [[ "$ENV_FILE" != /* ]]; then
  ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
fi
TARGET_BRANCH="${TRADE_OS_AUTO_PUBLISH_BRANCH:-main}"
AUTO_PUBLISH_TMPDIR="${TMPDIR:-/tmp}"
LOCK_DIR="$AUTO_PUBLISH_TMPDIR/trosa-auto-publish.lock"
DRY_RUN=0
USE_STAGED=0
COMMIT_MESSAGE="${TRADE_OS_AUTO_PUBLISH_COMMIT_MESSAGE:-}"
REQUESTED_FILES=()
TEST_DATA_DIR=""
STAGED_BY_SCRIPT=0

usage() {
  cat <<'EOF'
Usage:
  deploy/cloud/auto-publish.sh --message "说明本次变化" -- FILE [FILE ...]
  deploy/cloud/auto-publish.sh --staged --message "说明本次变化"
  deploy/cloud/auto-publish.sh --dry-run --message "说明本次变化" -- FILE [FILE ...]

The first form stages only the listed files. The --staged form uses an index
that was prepared by the caller. Both forms preserve unrelated worktree
changes and refuse to continue when tracked unstaged changes remain.
EOF
}

fail() {
  printf '自动发布未完成：%s\n' "$*" >&2
  exit 1
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ "$DRY_RUN" == 1 && "$STAGED_BY_SCRIPT" == 1 && ${#REQUESTED_FILES[@]} -gt 0 ]]; then
    (cd "$SOURCE_DIR" && git reset --quiet -- "${REQUESTED_FILES[@]}") || true
  fi
  if [[ -n "$TEST_DATA_DIR" && -d "$TEST_DATA_DIR" ]]; then
    rm -rf -- "$TEST_DATA_DIR"
  fi
  if [[ -d "$LOCK_DIR" ]]; then
    rmdir -- "$LOCK_DIR" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --message|-m)
      [[ $# -ge 2 ]] || fail "$1 需要一个 commit message"
      COMMIT_MESSAGE=$2
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --staged)
      USE_STAGED=1
      shift
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        REQUESTED_FILES+=("$1")
        shift
      done
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    -*)
      fail "未知参数：$1"
      ;;
    *)
      fail "文件路径必须放在 -- 后面；收到：$1"
      ;;
  esac
done

[[ -n "$COMMIT_MESSAGE" ]] || COMMIT_MESSAGE="update: Trosa $(date '+%Y-%m-%d %H:%M')"
[[ "$SOURCE_DIR" == /* ]] || fail '无法解析项目根目录'
[[ -r "$ENV_FILE" ]] || fail "找不到发布配置 $ENV_FILE；请先复制 workbench.env.example"
PROJECT_ROOT="$SOURCE_DIR"
# workbench.env is routing metadata only; keep the resolved repository root
# authoritative even if a local shell profile happens to define a same-named
# variable.
source "$ENV_FILE"
SOURCE_DIR="$PROJECT_ROOT"

PUBLIC_URL="${TRADE_OS_PUBLIC_URL:-https://app.trosa.space}"
case "$PUBLIC_URL" in
  http://*|https://*) ;;
  *) fail "无效的 TRADE_OS_PUBLIC_URL：$PUBLIC_URL" ;;
esac
PUBLIC_HEALTH_URL="${PUBLIC_URL%/}/api/network/ping"

if [[ "$USE_STAGED" == 1 && ${#REQUESTED_FILES[@]} -gt 0 ]]; then
  fail '--staged 不能和显式文件路径同时使用'
fi
if [[ "$USE_STAGED" != 1 && ${#REQUESTED_FILES[@]} -eq 0 ]]; then
  usage >&2
  fail '必须显式提供本次改动文件，或使用 --staged'
fi

if ! mkdir -- "$LOCK_DIR" 2>/dev/null; then
  fail "已有另一个自动发布正在运行（锁：$LOCK_DIR）"
fi

cd "$SOURCE_DIR"
current_branch="$(git symbolic-ref --quiet --short HEAD || true)"
[[ "$current_branch" == "$TARGET_BRANCH" ]] || fail "当前分支为 $current_branch，自动发布只允许 $TARGET_BRANCH"

origin_url="$(git remote get-url origin 2>/dev/null || true)"
[[ -n "$origin_url" ]] || fail '找不到 origin 远程仓库'

initial_staged="$(git diff --cached --name-only)"
if [[ -n "$initial_staged" && ${#REQUESTED_FILES[@]} -gt 0 ]]; then
  printf '当前 index 已有暂存文件：\n%s\n' "$initial_staged" >&2
  fail '为避免混入旧暂存内容，请先处理 index，再重新运行自动发布'
fi
if [[ -n "$initial_staged" && "$USE_STAGED" != 1 ]]; then
  fail '当前 index 已有暂存内容；请使用 --staged 明确采用它，或先处理 index'
fi

if [[ ${#REQUESTED_FILES[@]} -gt 0 ]]; then
  STAGED_BY_SCRIPT=1
  for path in "${REQUESTED_FILES[@]}"; do
    [[ -n "$path" ]] || fail '文件路径不能为空'
    case "$path" in
      .|..|./*|../*|*/../*|/*)
        fail "拒绝可能扩大范围的文件路径：$path"
        ;;
    esac
    git add -A -- "$path"
  done
fi

STAGED_FILES="$(git diff --cached --name-only --diff-filter=ACDMRTUXB)"
[[ -n "$STAGED_FILES" ]] || fail '没有可提交的暂存改动'

UNMERGED_FILES="$(git diff --cached --name-only --diff-filter=U)"
[[ -z "$UNMERGED_FILES" ]] || fail "index 中存在未解决冲突：$UNMERGED_FILES"

UNSTAGED_FILES="$(git diff --name-only)"
if [[ -n "$UNSTAGED_FILES" ]]; then
  printf '检测到未暂存的已跟踪文件：\n%s\n' "$UNSTAGED_FILES" >&2
  fail '为避免把用户的其他修改和本次发布混在一起，请先处理这些文件'
fi

while IFS= read -r path; do
  case "$path" in
    data|data/*|app/data|app/data/*|*.db|*.db-*|*.sqlite|*.sqlite-*|*.sqlite3|*.bak|*.wal|*.shm|*.log|.env|.env.*|deploy/cloud/workbench.env|deploy/macos/cloudflared.yml)
      fail "禁止把运行数据、密钥或本地环境文件放入发布 commit：$path"
      ;;
  esac
done <<< "$STAGED_FILES"

if ! git diff --cached --check; then
  fail '暂存 diff 存在空白字符错误'
fi

DB_SENSITIVE=0
DB_FILES=()
while IFS= read -r path; do
  case "$path" in
    migrations/*|db.py|postgres_compat.py|postgres_schema_contract.py|tools/unified_postgres_migration.py|tools/unified_postgres_import.py|deploy/postgres-production/*)
      DB_SENSITIVE=1
      DB_FILES+=("$path")
      ;;
  esac
done <<< "$STAGED_FILES"

if [[ "$DB_SENSITIVE" == 1 ]]; then
  DB_DIFF="$(git diff --cached --unified=0 -- "${DB_FILES[@]}")"
  if printf '%s\n' "$DB_DIFF" | grep -Eiq '^\+[^+].*(DROP[[:space:]]+(TABLE|TABLES|COLUMN|SCHEMA|DATABASE|INDEX)|TRUNCATE[[:space:]]+(TABLE|TABLES)|DELETE[[:space:]]+FROM|ALTER[[:space:]]+TABLE.*DROP)'; then
    if [[ "${TRADE_OS_AUTO_PUBLISH_ALLOW_DESTRUCTIVE_DB:-0}" != 1 ]]; then
      fail '检测到疑似破坏性数据库操作；需得到明确确认后设置 TRADE_OS_AUTO_PUBLISH_ALLOW_DESTRUCTIVE_DB=1 再发布'
    fi
    printf '%s\n' '警告：已显式允许疑似破坏性数据库操作；发布前仍会先备份。' >&2
  fi
fi

PYTHON_BIN="$SOURCE_DIR/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || fail "找不到项目 Python：$PYTHON_BIN"
command -v node >/dev/null 2>&1 || fail '找不到 node，无法执行前端语法检查'
command -v npm >/dev/null 2>&1 || fail '找不到 npm，无法执行浏览器扩展回归'

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

run_step_retry_once() {
  local label=$1
  shift
  local attempt=1
  local status=1
  printf '\n==> %s\n' "$label"
  while [[ "$attempt" -le 2 ]]; do
    if "$@"; then
      if [[ "$attempt" -eq 1 ]]; then
        printf '完成：%s\n' "$label"
      else
        printf '完成：%s（第 2 次运行通过）\n' "$label"
      fi
      return 0
    fi
    status=$?
    if [[ "$attempt" -eq 1 ]]; then
      printf '失败：%s（退出码 %s），将重跑一次以排除瞬时测试竞态\n' "$label" "$status" >&2
      attempt=2
      sleep 1
    else
      printf '失败：%s（两次运行均为退出码 %s）\n' "$label" "$status" >&2
      exit "$status"
    fi
  done
}

run_extension_tests() {
  (
    cd "$SOURCE_DIR/browser-extension"
    npm test
  )
}

run_python_regression() {
  if [[ -n "$TEST_DATA_DIR" && -d "$TEST_DATA_DIR" ]]; then
    rm -rf -- "$TEST_DATA_DIR"
  fi
  TEST_DATA_DIR="$(mktemp -d "${AUTO_PUBLISH_TMPDIR%/}/trosa-auto-publish-tests.XXXXXX")"
  export CRM_DB_PATH="$TEST_DATA_DIR"
  "$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py' -v
}

run_step_retry_once 'Python 回归测试' run_python_regression
run_step 'Python 语法检查' "$PYTHON_BIN" -m py_compile app.py db.py scheduler.py
run_step '前端 JavaScript 语法检查' node --check app/static/app.js
run_step '浏览器扩展回归测试' run_extension_tests

check_cloud_status() {
  local output
  if ! output="$(TRADE_OS_WORKBENCH_ENV="$ENV_FILE" "$SCRIPT_DIR/status-workbench.sh" 2>&1)"; then
    printf '%s\n' "$output" >&2
    return 1
  fi
  printf '%s\n' "$output"
  if ! printf '%s\n' "$output" | grep -Eq 'TROSA_MANAGER_STATUS .*app=active .*tunnel=active .*health=ok'; then
    printf '%s\n' '发布前 ECS 未同时满足 app=active、tunnel=active、health=ok。' >&2
    return 1
  fi
  printf '%s\n' '完成：发布前 ECS 状态'
}
run_step '发布前 ECS 状态' check_cloud_status

if [[ "$DB_SENSITIVE" == 1 ]]; then
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '\n[dry-run] 数据库敏感改动：将执行 backup-workbench.sh，当前跳过实际备份。\n'
  else
    run_step '数据库敏感改动备份' env TRADE_OS_WORKBENCH_ENV="$ENV_FILE" "$SCRIPT_DIR/backup-workbench.sh"
  fi
fi

run_step '同步远程 main 基线' git fetch --quiet origin "$TARGET_BRANCH"
remote_head="$(git rev-parse "refs/remotes/origin/$TARGET_BRANCH" 2>/dev/null || true)"
[[ -n "$remote_head" ]] || fail "无法读取 origin/$TARGET_BRANCH"
if ! git merge-base --is-ancestor "$remote_head" HEAD; then
  fail "本地分支落后或与 origin/$TARGET_BRANCH 分叉；为避免自动合并覆盖内容，本次停止"
fi

if [[ "$DRY_RUN" == 1 ]]; then
  printf '\nDRY_RUN_OK files=%s branch=%s\n' "$(printf '%s\n' "$STAGED_FILES" | tr '\n' ',')" "$TARGET_BRANCH"
  printf '%s\n' '未执行备份、commit、push 或 ECS 发布。'
  exit 0
fi

run_step '创建 commit' git commit -m "$COMMIT_MESSAGE"
COMMIT_SHA="$(git rev-parse HEAD)"
SHORT_SHA="${COMMIT_SHA:0:7}"

run_step '推送 GitHub main' git push origin "HEAD:$TARGET_BRANCH"
if ! remote_after="$(git ls-remote origin "refs/heads/$TARGET_BRANCH" | awk 'NR == 1 {print $1}')"; then
  fail '无法验证 GitHub main 的远程 commit'
fi
[[ "$remote_after" == "$COMMIT_SHA" ]] || fail "GitHub main 未确认到本次 commit：本地=$COMMIT_SHA 远程=$remote_after"

RELEASE_ID="${TRADE_OS_RELEASE_ID:-auto-$(date -u +%Y%m%d%H%M%S)-$SHORT_SHA}"
publish_output=""
publish_status=0
if publish_output="$(TRADE_OS_WORKBENCH_ENV="$ENV_FILE" TRADE_OS_SOURCE_DIR="$SOURCE_DIR" TRADE_OS_RELEASE_ID="$RELEASE_ID" "$SCRIPT_DIR/publish-workbench.sh" 2>&1)"; then
  :
else
  publish_status=$?
  printf '%s\n' "$publish_output" >&2
  printf 'GitHub 已保存 commit=%s，但 ECS 未确认上线；发布脚本应已自动保留上一 release。\n' "$COMMIT_SHA" >&2
  exit "$publish_status"
fi
printf '%s\n' "$publish_output"

public_ok=0
public_body=""
for attempt in 1 2 3 4 5 6; do
  public_body="$(curl --fail --silent --show-error --max-time 10 "$PUBLIC_HEALTH_URL" 2>/dev/null || true)"
  if printf '%s' "$public_body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' && printf '%s' "$public_body" | grep -Eq '"sela_sync_api"[[:space:]]*:[[:space:]]*"sela-v1"'; then
    public_ok=1
    break
  fi
  sleep 2
done

if [[ "$public_ok" != 1 ]]; then
  printf '%s\n' 'ECS 本机发布健康检查已通过，但公网健康接口暂未确认；未因可能的 Tunnel/网络抖动自动回滚。' >&2
  printf 'commit=%s release=%s public_health=unknown url=%s\n' "$COMMIT_SHA" "$RELEASE_ID" "$PUBLIC_HEALTH_URL" >&2
  exit 1
fi

printf '\nAUTO_PUBLISH_SUCCESS commit=%s branch=%s release=%s public_health=ok\n' "$COMMIT_SHA" "$TARGET_BRANCH" "$RELEASE_ID"
