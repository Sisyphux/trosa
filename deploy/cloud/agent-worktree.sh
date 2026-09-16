#!/usr/bin/env bash
# Trosa 多 Agent 任务隔离：一个任务 = 一个 git worktree + 一个独立分支。
#
# 背景：多个 Agent 共用同一个 working tree 时，各自的修改、暂存、测试会互相
# 污染，还会触发 auto-publish.sh 的保护门禁（index 非空 / 有未暂存改动就拒绝），
# 导致谁都发布不了。本脚本让每个任务在独立目录、独立分支上工作：
#
#   deploy/cloud/agent-worktree.sh create --task <id>   # 建隔离区 + agent/<id> 分支
#   deploy/cloud/agent-worktree.sh list                 # 看所有任务隔离区
#   deploy/cloud/agent-worktree.sh test --task <id>     # 在隔离区跑完整验证
#   deploy/cloud/agent-worktree.sh sync --task <id>     # 变基到最新 main
#   deploy/cloud/agent-worktree.sh publish --task <id> --message "说明"  # 合入并发布
#   deploy/cloud/agent-worktree.sh remove --task <id>   # 回收隔离区
#
# 隔离保证：
# - 工作区：worktree 目录在仓库之外（默认与仓库同级的 trosa-worktrees/），
#   改动、暂存、未跟踪文件互不可见；主工作区的未提交改动不受任何影响。
# - 测试：沿用 auto-publish.sh 的隔离模式（CRM_DB_PATH=临时目录），且 worktree
#   内即使有 stray 写入也只落在一次性目录里；测试无固定端口绑定，可并行跑。
# - 环境：复用主仓 .venv 与 browser-extension/node_modules（symlink），不复制、
#   不重装；workbench.env 从不复制进 worktree（密钥不跨区）。
#
# 发布保证（没有放宽任何门禁）：
# - publish 只接受：任务 worktree 完全干净（先 commit/push 分支）、主工作区
#   完全干净（有任何已跟踪改动就拒绝，避免卷入他人在途工作）。
# - 合入方式是 git merge --no-commit --no-ff，冲突则 abort，主分支保持原样。
# - 合入后全权委托未经修改的 auto-publish.sh --staged：本地回归、只读 ECS
#   状态、数据库备份、破坏性检查、提交、推送、trosa-release publish、公网
#   健康检查——全部照常执行。最终 ECS 上线的仍然是一个明确的 commit。
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_COMMON_DIR="$(cd "$SCRIPT_DIR" && git rev-parse --git-common-dir 2>/dev/null)"
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$SCRIPT_DIR/$GIT_COMMON_DIR" ;;
esac
MAIN_ROOT="$(cd "$GIT_COMMON_DIR/.." 2>/dev/null && pwd)"
[[ "$MAIN_ROOT" == /* ]] || { printf '任务隔离未完成：无法解析主仓库根目录\n' >&2; exit 1; }
WORKTREE_ROOT="${TRADE_OS_WORKTREE_ROOT:-$(dirname "$MAIN_ROOT")/trosa-worktrees}"
TARGET_BRANCH="${TRADE_OS_AUTO_PUBLISH_BRANCH:-main}"
AUTO_PUBLISH_TMPDIR="${TMPDIR:-/tmp}"
LOCK_DIR="$AUTO_PUBLISH_TMPDIR/trosa-auto-publish.lock"
TASK_TEST_DATA_DIR=""

fail() {
  printf '任务隔离未完成：%s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  agent-worktree.sh create --task <id> [--base <ref>] [--fetch-base]
  agent-worktree.sh list
  agent-worktree.sh test --task <id> [--quick]
  agent-worktree.sh sync --task <id> [--fetch-base]
  agent-worktree.sh publish --task <id> --message "说明本次变化"
  agent-worktree.sh remove --task <id> [--force] [--delete-branch]

<id> 只能包含字母、数字、点、下划线、连字符；对应分支为 agent/<id>，
隔离目录默认为 <仓库同级>/trosa-worktrees/<id>（可用
TRADE_OS_WORKTREE_ROOT 覆盖）。publish 要求任务区与主工作区都没有
已跟踪改动，合入后委托 auto-publish.sh --staged 走完全部现有门禁。
EOF
}

validate_task_id() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || fail "非法任务 id：$1（只允许字母数字点下划线连字符）"
  [[ "$1" != "$TARGET_BRANCH" && "$1" != HEAD ]] || fail "任务 id 不能是 $1"
}

branch_of() { printf 'agent/%s' "$1"; }
path_of() { printf '%s/%s' "$WORKTREE_ROOT" "$1"; }

# 输出任务 worktree 绝对路径；找不到则失败。
find_task_path() {
  local task=$1 branch path current_branch=""
  while IFS= read -r line; do
    case "$line" in
      worktree\ *) path="${line#worktree }" ;;
      branch\ *) current_branch="${line#branch refs/heads/}" ;;
      "") 
        if [[ "$current_branch" == "$(branch_of "$task")" ]]; then printf '%s' "$path"; return 0; fi
        current_branch="" ;;
    esac
  done < <(git -C "$MAIN_ROOT" worktree list --porcelain; printf '\n')
  if [[ "$current_branch" == "$(branch_of "$task")" ]]; then printf '%s' "$path"; return 0; fi
  fail "找不到任务 $task 的隔离区（先用 create 创建）"
}

require_clean_tracked() {
  local repo=$1 label=$2 staged unstaged
  staged="$(git -C "$repo" diff --cached --name-only)"
  unstaged="$(git -C "$repo" diff --name-only)"
  if [[ -n "$staged" || -n "$unstaged" ]]; then
    printf '任务隔离未完成：%s 存在已跟踪改动，请先提交：\n%s\n%s\n' "$label" "$staged" "$unstaged" >&2
    exit 1
  fi
}

cmd_create() {
  local task="" base="$TARGET_BRANCH" fetch_base=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --base) [[ $# -ge 2 ]] || fail '--base 需要一个 ref'; base=$2; shift 2 ;;
      --fetch-base) fetch_base=1; shift ;;
      *) fail "create 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'create 需要 --task <id>'
  validate_task_id "$task"
  git -C "$MAIN_ROOT" worktree prune
  git -C "$MAIN_ROOT" rev-parse --verify --quiet "refs/heads/$(branch_of "$task")" >/dev/null \
    && fail "分支 $(branch_of "$task") 已存在（换 id，或用 sync/remove 处理旧任务）"
  [[ -e "$(path_of "$task")" ]] && fail "目录 $(path_of "$task") 已存在"
  if [[ "$fetch_base" == 1 ]]; then
    git -C "$MAIN_ROOT" fetch --quiet origin "$TARGET_BRANCH" \
      || fail "fetch origin/$TARGET_BRANCH 失败（网络不可用时去掉 --fetch-base，用本地 $TARGET_BRANCH）"
    base="origin/$TARGET_BRANCH"
  fi
  git -C "$MAIN_ROOT" rev-parse --verify --quiet "$base" >/dev/null \
    || fail "基线 $base 不存在"
  [[ -d "$(dirname "$WORKTREE_ROOT")" ]] || fail "worktree 根目录的父目录不存在：$(dirname "$WORKTREE_ROOT")"
  mkdir -p -- "$WORKTREE_ROOT"
  git -C "$MAIN_ROOT" worktree add -b "$(branch_of "$task")" -- "$(path_of "$task")" "$base"
  local wt
  wt="$(path_of "$task")"
  if [[ -x "$MAIN_ROOT/.venv/bin/python" ]]; then
    ln -s -- "$MAIN_ROOT/.venv" "$wt/.venv"
  else
    printf '警告：主仓 .venv 不可用，隔离区测试前需先恢复主仓依赖。\n' >&2
  fi
  if [[ -d "$MAIN_ROOT/browser-extension/node_modules" ]]; then
    ln -s -- "$MAIN_ROOT/browser-extension/node_modules" "$wt/browser-extension/node_modules"
  else
    printf '警告：主仓 browser-extension/node_modules 缺失，扩展回归前需先 npm install。\n' >&2
  fi
  "$wt/.venv/bin/python" --version >/dev/null 2>&1 \
    || fail "隔离区 Python 自检失败：$wt/.venv"
  node --check "$wt/app/static/app.js" \
    || fail "隔离区前端自检失败"
  printf '\n任务隔离区已就绪：\n  目录：%s\n  分支：%s（基线 %s）\n' "$wt" "$(branch_of "$task")" "$base"
  printf '下一步：在该目录改代码、commit 到本任务分支；验证用 test，发布用 publish。\n'
}

cmd_list() {
  git -C "$MAIN_ROOT" worktree prune
  local path="" head="" branch="" first=1
  while IFS= read -r line; do
    case "$line" in
      worktree\ *) path="${line#worktree }" ;;
      HEAD\ *) head="${line#HEAD }" ;;
      branch\ *) branch="${line#branch }"; branch="${branch#refs/heads/}" ;;
      detached|prunable*) branch="$line" ;;
      "")
        [[ -z "$path" ]] && { path=""; head=""; branch=""; continue; }
        [[ "$first" == 1 ]] || printf '\n'
        first=0
        printf 'path=%s\nbranch=%s\nhead=%s' "$path" "${branch:-?}" "${head:0:12}"
        if [[ -d "$path" ]]; then
          if [[ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]]; then printf '\nstate=dirty'; else printf '\nstate=clean'; fi
        else
          printf '\nstate=missing'
        fi
        path=""; head=""; branch="" ;;
    esac
  done < <(git -C "$MAIN_ROOT" worktree list --porcelain; printf '\n')
}

cmd_test() {
  local task="" quick=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --quick) quick=1; shift ;;
      *) fail "test 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'test 需要 --task <id>'
  validate_task_id "$task"
  local wt python_bin
  wt="$(find_task_path "$task")"
  [[ -d "$wt" ]] || fail "隔离区目录缺失：$wt"
  python_bin="$wt/.venv/bin/python"
  [[ -x "$python_bin" ]] || fail "隔离区 Python 不可用（symlink 断裂时重建隔离区）"
  command -v node >/dev/null 2>&1 || fail '找不到 node'
  cleanup() {
    if [[ -n "$TASK_TEST_DATA_DIR" && -d "$TASK_TEST_DATA_DIR" ]]; then rm -rf -- "$TASK_TEST_DATA_DIR"; fi
  }
  trap cleanup EXIT
  printf '\n==> [%s] Python 语法检查\n' "$task"
  "$python_bin" -m py_compile app.py db.py scheduler.py serve.py serve_rehearsal.py
  printf '完成：Python 语法检查\n'
  printf '\n==> [%s] 前端 JavaScript 语法检查\n' "$task"
  (cd "$wt" && node --check app/static/app.js)
  printf '完成：前端 JavaScript 语法检查\n'
  if [[ "$quick" == 1 ]]; then printf '\n任务 %s 快速检查通过。\n' "$task"; return 0; fi
  command -v npm >/dev/null 2>&1 || fail '找不到 npm，无法执行浏览器扩展回归'
  printf '\n==> [%s] Python 回归测试（隔离数据目录）\n' "$task"
  TASK_TEST_DATA_DIR="$(mktemp -d "${AUTO_PUBLISH_TMPDIR%/}/trosa-task-tests.XXXXXX")"
  (cd "$wt" && CRM_DB_PATH="$TASK_TEST_DATA_DIR" "$python_bin" -m unittest discover -s tests -p 'test_*.py' -v)
  printf '完成：Python 回归测试\n'
  printf '\n==> [%s] 浏览器扩展回归测试\n' "$task"
  (cd "$wt/browser-extension" && npm test)
  printf '完成：浏览器扩展回归测试\n'
  printf '\n任务 %s 完整验证通过。\n' "$task"
}

cmd_sync() {
  local task="" fetch_base=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --fetch-base) fetch_base=1; shift ;;
      *) fail "sync 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'sync 需要 --task <id>'
  validate_task_id "$task"
  local wt
  wt="$(find_task_path "$task")"
  require_clean_tracked "$wt" "任务 $task"
  local base="$TARGET_BRANCH"
  if [[ "$fetch_base" == 1 ]]; then
    git -C "$MAIN_ROOT" fetch --quiet origin "$TARGET_BRANCH" || fail 'fetch 失败'
    base="origin/$TARGET_BRANCH"
  fi
  if git -C "$wt" rebase "$base"; then
    printf '任务 %s 已变基到 %s。\n' "$task" "$base"
  else
    git -C "$wt" rebase --abort || true
    fail "变基冲突，已回退；请进 $wt 手工解决后再 sync"
  fi
}

cmd_publish() {
  local task="" message=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --message|-m) [[ $# -ge 2 ]] || fail '--message 需要说明文字'; message=$2; shift 2 ;;
      *) fail "publish 未知参数：$1（发布只接受明确 commit，不接受文件列表之外的范围）" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'publish 需要 --task <id>'
  [[ -n "$message" ]] || fail 'publish 需要 --message "说明本次变化"'
  validate_task_id "$task"
  local wt branch
  wt="$(find_task_path "$task")"
  branch="$(branch_of "$task")"
  require_clean_tracked "$wt" "任务 $task（改动先 commit 到 $branch）"
  require_clean_tracked "$MAIN_ROOT" "主工作区（他人在途工作未处理，本次停止）"
  if [[ -d "$LOCK_DIR" ]]; then fail "已有另一个自动发布正在运行（锁：$LOCK_DIR），稍后重试"; fi
  git -C "$wt" push --force-with-lease origin "$branch" || fail "推送 $branch 失败"
  git -C "$MAIN_ROOT" fetch --quiet origin "$TARGET_BRANCH" || fail 'fetch 远端基线失败'
  local remote_head
  remote_head="$(git -C "$MAIN_ROOT" rev-parse "refs/remotes/origin/$TARGET_BRANCH")"
  git -C "$MAIN_ROOT" merge-base --is-ancestor "$remote_head" HEAD \
    || fail "本地 $TARGET_BRANCH 落后或与远端分叉；先处理主分支再发布"
  local merged=0
  abort_merge() {
    if [[ "$merged" == 1 ]] && git -C "$MAIN_ROOT" rev-parse --verify --quiet MERGE_HEAD >/dev/null; then
      git -C "$MAIN_ROOT" merge --abort || true
    fi
  }
  trap abort_merge EXIT
  if git -C "$MAIN_ROOT" merge --no-commit --no-ff -m "$message" "$branch"; then
    merged=1
  else
    fail "合入 $branch 冲突，已 abort；请进任务区 sync 变基解冲突后再发布"
  fi
  trap - EXIT
  # 合入结果已暂存；之后全部委托未经修改的 auto-publish.sh --staged，
  # 本脚本不再做任何提交、推送与发布动作，全部现有门禁照常执行。
  # 若它中途失败，用 merge --abort 恢复主分支（树原本干净，无损）。
  if bash "$MAIN_ROOT/deploy/cloud/auto-publish.sh" --staged --message "$message"; then
    printf '\n任务 %s 已合入并发布。\n' "$task"
  else
    abort_merge_after_fail() {
      git -C "$MAIN_ROOT" merge --abort 2>/dev/null || true
    }
    abort_merge_after_fail
    fail 'auto-publish 未完成（原因见上）；合入已回退，主分支保持原样'
  fi
}

cmd_remove() {
  local task="" force=0 delete_branch=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --force) force=1; shift ;;
      --delete-branch) delete_branch=1; shift ;;
      *) fail "remove 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'remove 需要 --task <id>'
  validate_task_id "$task"
  local wt
  wt="$(find_task_path "$task")"
  if [[ "$force" != 1 ]]; then
    require_clean_tracked "$wt" "任务 $task（未提交改动会丢失；确认丢弃请加 --force）"
    git -C "$MAIN_ROOT" worktree remove -- "$wt"
  else
    git -C "$MAIN_ROOT" worktree remove --force -- "$wt"
  fi
  if [[ "$delete_branch" == 1 ]]; then
    if [[ "$force" == 1 ]]; then git -C "$MAIN_ROOT" branch -D -- "$(branch_of "$task")"
    else git -C "$MAIN_ROOT" branch -d -- "$(branch_of "$task")" || fail "分支 $(branch_of "$task") 尚未合入；确认丢弃请加 --force"
    fi
  fi
  git -C "$MAIN_ROOT" worktree prune
  printf '任务 %s 的隔离区已回收。\n' "$task"
}

[[ $# -ge 1 ]] || { usage; exit 1; }
command=$1
shift
case "$command" in
  create) cmd_create "$@" ;;
  list) cmd_list "$@" ;;
  test) cmd_test "$@" ;;
  sync) cmd_sync "$@" ;;
  publish) cmd_publish "$@" ;;
  remove) cmd_remove "$@" ;;
  --help|-h|help) usage ;;
  *) fail "未知命令：$command（用 --help 查看）" ;;
esac
