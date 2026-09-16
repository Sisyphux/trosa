#!/usr/bin/env bash
# Trosa 多 Agent 任务隔离：一个任务 = 一个 git worktree + 一个独立分支。
#
# 背景：多个 Agent 共用同一个 working tree 时，各自的修改、暂存、测试会互相
# 污染，而且发布入口被迫理解“发布哪些文件”，于是谁都发布不了。本脚本让每个
# 任务在独立目录、独立分支上工作，并把发布输入收敛为 commit：
#
#   deploy/cloud/agent-worktree.sh create --task <id>   # 建隔离区 + agent/<id> 分支
#   deploy/cloud/agent-worktree.sh list                 # 看所有任务隔离区
#   deploy/cloud/agent-worktree.sh test --task <id>     # 在隔离区跑完整验证
#   deploy/cloud/agent-worktree.sh sync --task <id>     # 变基到最新 main
#   deploy/cloud/agent-worktree.sh publish --task <id>  # 发布本任务的 commit
#   deploy/cloud/agent-worktree.sh remove --task <id>   # 回收隔离区
#
# 隔离保证：
# - 工作区：worktree 目录在仓库之外（默认与仓库同级的 trosa-worktrees/），
#   改动、暂存、未跟踪文件互不可见；主工作区的未提交改动不受任何影响。
# - 测试：委托 deploy/cloud/release-test.sh（与发布候选同一份门禁），
#   CRM_DB_PATH 指向一次性目录。
# - 环境：复用主仓 .venv 与 browser-extension/node_modules（symlink），不复制、
#   不重装；workbench.env 从不复制进 worktree（密钥不跨区）。
#
# 发布保证（没有放宽任何门禁）：
# - publish 只接受：任务 worktree 完全干净（改动必须先 commit 到 agent/<id>）。
# - 主工作区不参与发布，也不再需要干净：publish 委托 release-commit.sh --branch
#   agent/<id>，在基于 origin/main 的临时 release worktree 里 cherry-pick 本任务
#   的 commit，跑完整门禁后再推送并发布。任何在途改动、脏 index、未跟踪文件都
#   不会被读取、暂存或修改。
# - 测试与发布候选使用同一份 release-test.sh，避免“开发机绿、候选红”。
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
  agent-worktree.sh publish --task <id> [--message "仅记录用的说明"]
  agent-worktree.sh remove --task <id> [--force] [--delete-branch]

<id> 只能包含字母、数字、点、下划线、连字符；对应分支为 agent/<id>，
隔离目录默认为 <仓库同级>/trosa-worktrees/<id>（可用
TRADE_OS_WORKTREE_ROOT 覆盖）。

test 委托 release-test.sh，与发布候选使用同一份门禁。
publish 只要求任务区干净（先把改动 commit 到 agent/<id>），随后委托
release-commit.sh --branch agent/<id>：在基于 origin/main 的临时 release
worktree 里 cherry-pick 本任务的 commit、跑完整门禁、推送并发布。调用者
工作区（含主工作区的在途改动）不参与发布，也不会被修改。发布说明应写在
commit message 里；--message 仅用于终端记录。
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

require_clean() {
  local repo=$1 label=$2 status
  status="$(git -C "$repo" status --porcelain --untracked-files=all)"
  if [[ -n "$status" ]]; then
    printf '任务隔离未完成：%s 不是干净 worktree，请先提交或处理：\n%s\n' "$label" "$status" >&2
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
  if [[ -x "$wt/.venv/bin/python" ]]; then
    "$wt/.venv/bin/python" --version >/dev/null 2>&1 \
      || fail "隔离区 Python 自检失败：$wt/.venv"
  fi
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
  local wt args=() gate
  wt="$(find_task_path "$task")"
  [[ -d "$wt" ]] || fail "隔离区目录缺失：$wt"
  # If the task branch already contains the gate, test that exact version;
  # otherwise use the repository's committed gate while bootstrapping it.
  gate="$wt/deploy/cloud/release-test.sh"
  [[ -r "$gate" ]] || gate="$MAIN_ROOT/deploy/cloud/release-test.sh"
  [[ -r "$gate" ]] || fail "找不到发布门禁 $MAIN_ROOT/deploy/cloud/release-test.sh"
  args=(--dir "$wt")
  if [[ "$quick" == 1 ]]; then args+=(--quick); fi
  bash "$gate" ${args[@]+"${args[@]}"}
  printf '任务 %s 验证完成（门禁实现：release-test.sh）。\n' "$task"
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
  require_clean "$wt" "任务 $task"
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
      --message|-m) [[ $# -ge 2 ]] || fail '--message 需要文字'; message=$2; shift 2 ;;
      *) fail "publish 未知参数：$1（发布输入是 commit，不接受文件清单）" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'publish 需要 --task <id>'
  validate_task_id "$task"
  local wt branch head
  wt="$(find_task_path "$task")"
  branch="$(branch_of "$task")"
  require_clean "$wt" "任务 $task（改动先 commit 到 $branch）"
  head="$(git -C "$wt" rev-parse HEAD)"
  if [[ -n "$message" ]]; then
    printf '说明（仅记录用；实际 commit message 来自任务分支的 commit）：%s\n' "$message"
  fi
  [[ -r "$MAIN_ROOT/deploy/cloud/auto-publish.sh" ]] \
    || fail "找不到发布入口 $MAIN_ROOT/deploy/cloud/auto-publish.sh"
  # The branch remains a local task artifact. auto-publish resolves its commits
  # from the shared object database, builds a clean release worktree, and only
  # pushes the resulting release candidate to main.
  printf '\n发布任务 %s：%s（HEAD %s）→ origin/%s\n' "$task" "$branch" "${head:0:9}" "$TARGET_BRANCH"
  bash "$MAIN_ROOT/deploy/cloud/auto-publish.sh" --branch "$branch"
  printf '\n任务 %s 已发布。\n' "$task"
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
    require_clean "$wt" "任务 $task（未提交改动会丢失；确认丢弃请加 --force）"
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
