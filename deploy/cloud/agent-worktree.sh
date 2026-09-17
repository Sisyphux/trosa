#!/usr/bin/env bash
# Trosa 多 Agent 任务隔离：一个任务 = 一个 git worktree + 一个独立分支。
#
# 背景：多个 Agent 共用同一个 working tree 时，各自的修改、暂存、测试会互相
# 污染，而且发布入口被迫理解“发布哪些文件”，于是谁都发布不了。本脚本让每个
# 任务在独立目录、独立分支上工作，并把发布输入收敛为 commit：
#
#   deploy/cloud/agent-worktree.sh status               # 判断“我在哪个环境/哪个任务”
#   deploy/cloud/agent-worktree.sh create --task <id>   # 建隔离区 + agent/<id> 分支
#   deploy/cloud/agent-worktree.sh adopt --task <id>    # 把主工作区在途改动搬进隔离区
#   deploy/cloud/agent-worktree.sh preflight            # 并发体检：脏主区 / 迁移编号冲突
#   deploy/cloud/agent-worktree.sh list                 # 看所有任务隔离区
#   deploy/cloud/agent-worktree.sh test --task <id>     # 在隔离区跑完整验证
#   deploy/cloud/agent-worktree.sh sync --task <id>     # 变基到最新 main
#   deploy/cloud/agent-worktree.sh publish --task <id>  # 发布本任务的 commit
#   deploy/cloud/agent-worktree.sh remove --task <id>   # 回收隔离区
#
# 任务边界：
# - create 会在共享 git 目录写一份任务清单（trosa-tasks/<id>.json：负责人、目标、
#   修改范围、预留迁移编号），status 据此告诉 Agent“当前目录是不是我的任务”。
# - 主工作区保持集成/验收/发布角色；一旦出现无法归属的在途改动，用 adopt 整体
#   搬进任务隔离区，而不是继续在主工作区堆叠。
# - 新迁移编号在 create 时统一预留（跨主工作区与所有隔离区取下一个空号），并由
#   tools/check_migrations.py 在每棵树、每次发布前校验唯一且连续。
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
  agent-worktree.sh status
  agent-worktree.sh preflight
  agent-worktree.sh create --task <id> [--base <ref>] [--fetch-base]
                           [--owner <name>] [--goal <text>] [--scope <text>]
                           [--no-reserve-migration]
  agent-worktree.sh adopt --task <id> [--path <repo-relative> ...]
                          [--owner <name>] [--goal <text>] [--scope <text>]
  agent-worktree.sh list
  agent-worktree.sh test --task <id> [--quick]
  agent-worktree.sh sync --task <id> [--fetch-base]
  agent-worktree.sh publish --task <id> [--message "仅记录用的说明"]
  agent-worktree.sh remove --task <id> [--force] [--delete-branch]

<id> 只能包含字母、数字、点、下划线、连字符；对应分支为 agent/<id>，
隔离目录默认为 <仓库同级>/trosa-worktrees/<id>（可用
TRADE_OS_WORKTREE_ROOT 覆盖）。

status    在任意工作树里运行，报告当前环境、任务归属、未提交改动与预留迁移号。
preflight 并发体检：主工作区是否干净、各任务是否脏、迁移编号是否冲突。
create    建隔离区，写任务清单并预留下一个迁移编号。
adopt     把主工作区的在途改动（默认全部；可用 --path 限定）整体搬进新任务区，
          原始改动会保留为 stash 备份，主工作区恢复干净。路径按主工作区根解析。
test      委托 release-test.sh，与发布候选使用同一份门禁。
publish   只要求任务区干净（先把改动 commit 到 agent/<id>），随后委托
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

# 任务清单存放在共享 git 目录，而不是工作树内：它不进入任何 commit、不会让
# require_clean 失败，也不会泄露到发布产物。
TASK_META_DIR="$GIT_COMMON_DIR/trosa-tasks"
task_meta_path() { printf '%s/%s.json' "$TASK_META_DIR" "$1"; }

# 当前分支对应的任务 id；不是任务分支则输出空。
task_of_branch() {
  local branch=$1
  case "$branch" in
    agent/*) printf '%s' "${branch#agent/}" ;;
    *) printf '' ;;
  esac
}

# 找出所有工作树（主工作区 + 各隔离区） migrations/ 下的迁移文件名。
all_migration_basenames() {
  local line wt file
  while IFS= read -r line; do
    case "$line" in
      worktree\ *)
        wt="${line#worktree }"
        [[ -d "$wt/migrations" ]] || continue
        for file in "$wt"/migrations/*.sql; do
          [[ -e "$file" ]] || continue
          printf '%s\n' "${file##*/}"
        done
        ;;
    esac
  done < <(git -C "$MAIN_ROOT" worktree list --porcelain)
}

# 已有任务清单里预留的迁移编号；用于让新任务避开同机其它任务。
reserved_migration_numbers() {
  [[ -d "$TASK_META_DIR" ]] || return 0
  python3 - "$TASK_META_DIR" <<'PY'
import glob
import json
import os
import sys

for path in sorted(glob.glob(os.path.join(sys.argv[1], "*.json"))):
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        continue
    value = str(doc.get("reserved_migration") or "")
    if value.isdigit():
        print(value)
PY
}

# 跨主工作区、所有隔离区以及已预留的任务清单取下一个未占用的迁移编号，
# 避免两个并行任务抢同一个号。
next_migration_number() {
  local name number max=0
  while IFS= read -r number; do
    [[ "$number" =~ ^[0-9]+$ ]] || continue
    number=$((10#$number))
    (( number > max )) && max=$number
  done < <(
    while IFS= read -r name; do printf '%s\n' "${name%%_*}"; done < <(all_migration_basenames)
    reserved_migration_numbers
  )
  printf '%04d' $((max + 1))
}

write_task_meta() {
  local task=$1 branch=$2 wt=$3 base=$4 owner=$5 goal=$6 scope=$7 reserved=$8
  command -v python3 >/dev/null 2>&1 || fail '找不到 python3，无法写任务清单'
  mkdir -p -- "$TASK_META_DIR"
  python3 - "$(task_meta_path "$task")" "$task" "$branch" "$wt" "$base" \
    "$owner" "$goal" "$scope" "$reserved" <<'PY'
import datetime
import json
import sys

(path, task, branch, wt, base, owner, goal, scope, reserved) = sys.argv[1:10]
doc = {
    "task": task,
    "branch": branch,
    "path": wt,
    "base": base,
    "owner": owner,
    "goal": goal,
    "scope": scope,
    "reserved_migration": reserved,
    "created_at": datetime.datetime.now(
        datetime.timezone.utc
    ).astimezone().isoformat(timespec="seconds"),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

print_task_meta() {
  local task=$1 meta
  meta="$(task_meta_path "$task")"
  if [[ ! -r "$meta" ]]; then
    printf '  任务清单：无（未通过 create/adopt 创建）\n'
    return 0
  fi
  python3 - "$meta" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = json.load(handle)
for key, label in (
    ("owner", "负责人"),
    ("goal", "目标"),
    ("scope", "修改范围"),
    ("reserved_migration", "预留迁移编号"),
    ("base", "基线"),
    ("created_at", "创建时间"),
):
    value = doc.get(key)
    if value:
        print(f"  {label}：{value}")
PY
}

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
  local task="" base="$TARGET_BRANCH" fetch_base=0 owner="" goal="" scope="" reserve=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --base) [[ $# -ge 2 ]] || fail '--base 需要一个 ref'; base=$2; shift 2 ;;
      --fetch-base) fetch_base=1; shift ;;
      --owner) [[ $# -ge 2 ]] || fail '--owner 需要一个值'; owner=$2; shift 2 ;;
      --goal) [[ $# -ge 2 ]] || fail '--goal 需要文字'; goal=$2; shift 2 ;;
      --scope) [[ $# -ge 2 ]] || fail '--scope 需要文字'; scope=$2; shift 2 ;;
      --no-reserve-migration) reserve=0; shift ;;
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
  local reserved=""
  if [[ "$reserve" == 1 ]]; then
    reserved="$(next_migration_number)"
  fi
  write_task_meta "$task" "$(branch_of "$task")" "$wt" "$base" "$owner" "$goal" "$scope" "$reserved"
  printf '\n任务隔离区已就绪：\n  目录：%s\n  分支：%s（基线 %s）\n' "$wt" "$(branch_of "$task")" "$base"
  print_task_meta "$task"
  if [[ -n "$reserved" ]]; then
    printf '  下一个迁移编号：%s（无数据库改动时忽略）\n' "$reserved"
  fi
  printf '下一步：在该目录改代码、commit 到本任务分支；验证用 test，发布用 publish。\n'
  printf '开始前可在隔离区内运行 status，确认这里就是你的任务。\n'
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
        local list_task
        list_task="$(task_of_branch "$branch")"
        if [[ -n "$list_task" ]]; then
          printf '\ntask=%s' "$list_task"
          if [[ -r "$(task_meta_path "$list_task")" ]]; then
            python3 - "$(task_meta_path "$list_task")" <<'PY' 2>/dev/null || true
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = json.load(handle)
parts = []
if doc.get("owner"):
    parts.append(f"owner={doc['owner']}")
if doc.get("reserved_migration"):
    parts.append(f"reserved={doc['reserved_migration']}")
if doc.get("goal"):
    parts.append(f"goal={doc['goal']}")
if parts:
    print(" " + " ".join(parts))
PY
          fi
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

cmd_status() {
  local top branch task dirty_count
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$top" ]] || fail '当前目录不在任何 Git 工作树中'
  branch="$(git -C "$top" symbolic-ref --short -q HEAD || printf 'detached')"
  task="$(task_of_branch "$branch")"
  if [[ "$top" == "$MAIN_ROOT" ]]; then
    printf '环境：主工作区（集成 / 验收 / 发布）\n'
    printf '  路径：%s\n  分支：%s\n' "$top" "$branch"
    dirty_count="$(git -C "$top" status --porcelain --untracked-files=all | wc -l | tr -d ' ')"
    if [[ "$dirty_count" == 0 ]]; then
      printf '  未提交改动：无\n'
    else
      printf '  未提交改动：%s 项（无法归属时用 adopt 移入任务隔离区）\n' "$dirty_count"
      git -C "$top" status --short
    fi
    printf '  规则：不要在主工作区开发；先 create 一个任务区再改代码。\n'
  elif [[ -n "$task" ]]; then
    printf '环境：任务隔离区\n'
    printf '  路径：%s\n  分支：%s\n  任务：%s\n' "$top" "$branch" "$task"
    print_task_meta "$task"
    dirty_count="$(git -C "$top" status --porcelain --untracked-files=all | wc -l | tr -d ' ')"
    printf '  未提交改动：%s 项（只属于本任务，不影响其它任务）\n' "$dirty_count"
  else
    printf '环境：游离工作树（不是主工作区，也不是 agent/* 任务分支）\n'
    printf '  路径：%s\n  分支：%s\n' "$top" "$branch"
    printf '  注意：该目录不受任务隔离规则保护。\n'
  fi
}

cmd_preflight() {
  local problems=0 line list_task cur_path cur_branch state
  printf '== 并发开发体检 ==\n'

  printf '\n[主工作区]\n'
  local main_dirty
  main_dirty="$(git -C "$MAIN_ROOT" status --porcelain --untracked-files=all)"
  if [[ -n "$main_dirty" ]]; then
    printf '  主工作区不干净：\n'
    printf '%s\n' "$main_dirty" | sed 's/^/    /'
    printf '  → 请用 adopt 把在途改动移入任务隔离区，保持主工作区只做集成。\n'
    problems=$((problems + 1))
  else
    printf '  干净（符合“只做集成/验收/发布”的角色）。\n'
  fi

  printf '\n[任务隔离区]\n'
  while IFS= read -r line; do
    case "$line" in
      worktree\ *) cur_path="${line#worktree }" ;;
      branch\ *) cur_branch="${line#branch refs/heads/}" ;;
      "")
        if [[ -n "$cur_path" && "$cur_path" != "$MAIN_ROOT" ]]; then
          list_task="$(task_of_branch "$cur_branch")"
          if [[ -d "$cur_path" ]]; then
            if [[ -n "$(git -C "$cur_path" status --porcelain 2>/dev/null)" ]]; then state="dirty"; else state="clean"; fi
          else
            state="missing"
          fi
          printf '  %s  branch=%s  state=%s\n' "$cur_path" "$cur_branch" "$state"
          if [[ -n "$list_task" ]]; then print_task_meta "$list_task"; fi
        fi
        cur_path=""; cur_branch="" ;;
    esac
  done < <(git -C "$MAIN_ROOT" worktree list --porcelain; printf '\n')

  printf '\n[迁移编号]\n'
  local conflicts
  conflicts="$(all_migration_basenames | python3 -c '
import collections, sys
by_number = collections.defaultdict(set)
for name in sys.stdin:
    name = name.strip()
    if name:
        by_number[name[:4]].add(name)
for number in sorted(by_number):
    if len(by_number[number]) > 1:
        print(f"  {number}: " + ", ".join(sorted(by_number[number])))
')"
  if [[ -n "$conflicts" ]]; then
    printf '  发现编号冲突（两个任务抢同一个迁移号，合并前必须改名）：\n%s\n' "$conflicts"
    problems=$((problems + 1))
  else
    printf '  未发现跨任务编号冲突（每棵树内仍需通过 check_migrations.py）。\n'
  fi

  local dup_reserved
  dup_reserved="$(reserved_migration_numbers | sort | uniq -d)"
  if [[ -n "$dup_reserved" ]]; then
    printf '  多个活跃任务预留了同一编号（合并前必须错开）：\n'
    printf '%s\n' "$dup_reserved" | sed 's/^/    /'
    problems=$((problems + 1))
  fi

  printf '\n'
  if [[ "$problems" != 0 ]]; then
    printf '体检结论：%s 项需要处理。\n' "$problems"
    return 1
  fi
  printf '体检结论：通过。\n'
  return 0
}

cmd_adopt() {
  local task="" owner="" goal="" scope="" msg=""
  local paths=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --path) [[ $# -ge 2 ]] || fail '--path 需要一个路径'; paths+=("$2"); shift 2 ;;
      --owner) [[ $# -ge 2 ]] || fail '--owner 需要一个值'; owner=$2; shift 2 ;;
      --goal) [[ $# -ge 2 ]] || fail '--goal 需要文字'; goal=$2; shift 2 ;;
      --scope) [[ $# -ge 2 ]] || fail '--scope 需要文字'; scope=$2; shift 2 ;;
      --message|-m) [[ $# -ge 2 ]] || fail '--message 需要文字'; msg=$2; shift 2 ;;
      *) fail "adopt 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'adopt 需要 --task <id>'
  validate_task_id "$task"

  # --path 相对主工作区根解析；不给则搬运全部在途改动。
  local rel
  local rels=()
  for rel in ${paths[@]+"${paths[@]}"}; do
    rel="${rel#"$MAIN_ROOT"/}"
    rels+=("$rel")
  done

  local status
  if [[ ${#rels[@]} -gt 0 ]]; then
    status="$(git -C "$MAIN_ROOT" status --porcelain --untracked-files=all -- ${rels[@]+"${rels[@]}"})"
  else
    status="$(git -C "$MAIN_ROOT" status --porcelain --untracked-files=all)"
  fi
  [[ -n "$status" ]] || fail '主工作区没有可搬运的改动（或 --path 指定的路径没有改动）'

  local wt
  if git -C "$MAIN_ROOT" rev-parse --verify --quiet "refs/heads/$(branch_of "$task")" >/dev/null; then
    wt="$(find_task_path "$task")"
    require_clean "$wt" "任务 $task"
    printf '复用已存在的任务隔离区：%s\n' "$wt"
  else
    cmd_create --task "$task" --owner "$owner" --goal "$goal" --scope "$scope" --no-reserve-migration
    wt="$(find_task_path "$task")"
  fi

  local stash_msg="adopt:$task"
  if [[ -n "$msg" ]]; then stash_msg="adopt:$task: $msg"; fi
  if [[ ${#rels[@]} -gt 0 ]]; then
    git -C "$MAIN_ROOT" stash push --include-untracked -m "$stash_msg" -- "${rels[@]}" \
      || fail 'stash 失败，主工作区未改变'
  else
    git -C "$MAIN_ROOT" stash push --include-untracked -m "$stash_msg" \
      || fail 'stash 失败，主工作区未改变'
  fi
  local stash_sha
  stash_sha="$(git -C "$MAIN_ROOT" rev-parse refs/stash)"
  printf '\n已把改动保存为 stash %s（保留，不删除）。\n' "${stash_sha:0:9}"

  if git -C "$wt" stash apply "$stash_sha"; then
    printf '改动已应用到任务隔离区：%s\n' "$wt"
  else
    printf '应用失败，正在把改动放回主工作区...\n' >&2
    git -C "$MAIN_ROOT" stash apply "$stash_sha" || true
    fail '无法把改动应用到隔离区；已尝试恢复主工作区（stash 备份仍保留）'
  fi

  # 采纳后重新预留正确编号（采纳的迁移可能已占用下一个号）。
  local reserved
  reserved="$(next_migration_number)"
  write_task_meta "$task" "$(branch_of "$task")" "$wt" "$TARGET_BRANCH" "$owner" "$goal" "$scope" "$reserved"

  printf '\n主工作区已恢复干净。原始改动作为备份 stash 保留：\n'
  printf '  查看：git -C %s stash list\n' "$MAIN_ROOT"
  printf '  确认隔离区无误后清理：git -C %s stash drop stash@{0}\n' "$MAIN_ROOT"
  printf '下一步：cd %s，用 status 确认本任务，再 commit 到 %s。\n' "$wt" "$(branch_of "$task")"
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
  rm -f -- "$(task_meta_path "$task")"
  printf '任务 %s 的隔离区已回收。\n' "$task"
}

[[ $# -ge 1 ]] || { usage; exit 1; }
command=$1
shift
case "$command" in
  status) cmd_status "$@" ;;
  preflight) cmd_preflight "$@" ;;
  create) cmd_create "$@" ;;
  adopt) cmd_adopt "$@" ;;
  list) cmd_list "$@" ;;
  test) cmd_test "$@" ;;
  sync) cmd_sync "$@" ;;
  publish) cmd_publish "$@" ;;
  remove) cmd_remove "$@" ;;
  --help|-h|help) usage ;;
  *) fail "未知命令：$command（用 --help 查看）" ;;
esac
