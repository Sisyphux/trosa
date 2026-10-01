#!/usr/bin/env bash
# Trosa 多 Agent 任务隔离：一个任务 = 一个 git worktree + 一个独立分支。
#
# 背景：多个 Agent 共用同一个 working tree 时，各自的修改、暂存、测试会互相
# 污染，而且发布入口被迫理解“发布哪些文件”，于是谁都发布不了。本脚本让每个
# 任务在独立目录、独立分支上工作，并把发布输入收敛为 commit：
#
#   deploy/cloud/agent-worktree.sh status               # 判断“我在哪个环境/哪个任务”
#   deploy/cloud/agent-worktree.sh guard                # 开发任务开始前的入口闸门（dev/review）
#   deploy/cloud/agent-worktree.sh create --task <id>   # 建隔离区 + agent/<id> 分支
#   deploy/cloud/agent-worktree.sh adopt --task <id>    # 把主工作区在途改动搬进隔离区
#   deploy/cloud/agent-worktree.sh preflight            # 并发体检：脏主区 / 迁移编号冲突
#   deploy/cloud/agent-worktree.sh list                 # 看所有任务隔离区
#   deploy/cloud/agent-worktree.sh test --task <id>     # 在隔离区跑完整验证
#   deploy/cloud/agent-worktree.sh gate --task <id>     # 只读检查任务是否 ready
#   deploy/cloud/agent-worktree.sh reconcile --task <id># 消解迁移编号碰撞
#   deploy/cloud/agent-worktree.sh hooks                # 刷新入口隔离护栏
#   deploy/cloud/agent-worktree.sh sync --task <id>     # 消解碰撞 + 变基到最新 main
#   deploy/cloud/agent-worktree.sh publish --task <id>  # 发布本任务的 commit
#   deploy/cloud/agent-worktree.sh remove --task <id>   # 回收隔离区
#
# 入口隔离（硬护栏）：
# - 版本化 pre-commit（deploy/cloud/git-hooks/pre-commit）安装到共享 git 目录，
#   dev/review 角色在集成分支（main）上的提交会被拒绝，必须先进入 agent/<id> 隔离区；
#   release 角色与未设置角色（人工）不受影响。
# - guard 是开发任务开始前的闸门：dev/review 角色在主工作区（或任何非 agent/<id>
#   目录）会被明确拒绝，要求先 create/adopt；不等到 commit 才报错，避免主工作区
#   先被写脏。release/人工集成与只读 status 不受影响。
# - create/adopt 只能在主工作区执行；不能在任务隔离区里再建任务。
#
# 任务边界：
# - create 会在共享 git 目录写一份任务清单（trosa-tasks/<id>.json：负责人、目标、
#   修改范围、预留迁移编号），status 据此告诉 Agent“当前目录是不是我的任务”。
# - 主工作区保持集成/验收/发布角色；一旦出现无法归属的在途改动，用 adopt 整体
#   搬进任务隔离区，而不是继续在主工作区堆叠。
# - 新迁移编号在 create 时统一预留（跨主工作区与所有隔离区取下一个空号），
#   sync/publish 再用 tools/reconcile_migrations.py 自动消解并行编号碰撞。
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
# - publish 只接受真正 ready 的任务：任务 worktree 完全干净、门禁证据对应当前 HEAD、
#   HEAD 已包含最新 origin/main；否则先 sync + test。
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
# 发布配置解析与发布角色边界（TRADE_OS_AGENT_ROLE）由这一份共享实现提供。
# shellcheck source=release-env.sh
source "$SCRIPT_DIR/release-env.sh"
# 可移植锁与原子迁移编号预留。
# shellcheck source=lib-release-lock.sh
source "$SCRIPT_DIR/lib-release-lock.sh"
# 发布门禁对象身份与已验收树账本（release 侧按 tree hash 复用，不读任务目录）。
# shellcheck source=lib-release-gate.sh
source "$SCRIPT_DIR/lib-release-gate.sh"

fail() {
  printf '任务隔离未完成：%s\n' "$*" >&2
  exit 1
}

# 迁移编号预留锁：确保“读最大编号 → 写任务清单”是原子的，两个并发 create/adopt
# 不会拿到同一个号。锁在脚本退出时兜底释放，避免异常路径长期占用。
MIGRATION_LOCK_DIR=""
release_migration_lock() {
  if [[ -n "$MIGRATION_LOCK_DIR" ]]; then
    trosa_lock_release "$MIGRATION_LOCK_DIR"
    MIGRATION_LOCK_DIR=""
  fi
}
trap release_migration_lock EXIT

begin_migration_lock() {
  MIGRATION_LOCK_DIR="$TASK_META_DIR/.reserve.lock"
  mkdir -p -- "$TASK_META_DIR"
  trosa_lock_acquire "$MIGRATION_LOCK_DIR" 30 120 \
    || fail "无法获取迁移编号预留锁 ${MIGRATION_LOCK_DIR}（另一个任务正在预留，请稍后重试）"
}

usage() {
  cat <<'EOF'
Usage:
  agent-worktree.sh status
  agent-worktree.sh guard
  agent-worktree.sh preflight
  agent-worktree.sh start --task <id> [--base <ref>] [--fetch-base]
                          [--owner <name>] [--goal <text>] [--scope <text>]
  agent-worktree.sh create --task <id> [--base <ref>] [--fetch-base]
                           [--owner <name>] [--goal <text>] [--scope <text>]
                           [--reserve-migration]
  agent-worktree.sh adopt --task <id> [--path <repo-relative> ...]
                          [--owner <name>] [--goal <text>] [--scope <text>]
  agent-worktree.sh reserve-migration --task <id>
  agent-worktree.sh list
  agent-worktree.sh test --task <id> [--quick | --full]
  agent-worktree.sh ship --task <id> [--offline]
  agent-worktree.sh evidence --task <id>
  agent-worktree.sh flakes [--limit <n>]
  agent-worktree.sh gate --task <id>
  agent-worktree.sh reconcile --task <id>
  agent-worktree.sh hooks
  agent-worktree.sh sync --task <id> [--offline]
  agent-worktree.sh publish --task <id> [--message "仅记录用的说明"]
  agent-worktree.sh remove --task <id> [--force] [--delete-branch]

<id> 只能包含字母、数字、点、下划线、连字符；对应分支为 agent/<id>，
隔离目录默认为 <仓库同级>/trosa-worktrees/<id>（可用
TRADE_OS_WORKTREE_ROOT 覆盖）。

status    在任意工作树里运行，报告当前环境、任务归属、未提交改动与预留迁移号。
guard     开发任务开始前的入口闸门：dev/review 角色在主工作区（或非 agent/<id>
          目录）会被拒绝，要求先建隔离区；release/人工集成不受影响。只读，不改动
          任何文件。若当前是桌面端会话的 claude/* worktree，会直接提示在该目录运行
          start 建区（无需手动切到主工作区）。
preflight 并发体检：主工作区是否干净、各任务是否脏、迁移编号是否冲突。冲突按
          “与本任务相关 / 无关”分级：只有涉及本任务新增迁移或本任务预留号的冲突
          才阻断，其它既有冲突降级为一行提示。
start     开发方入口（推荐）：合并 guard + status + preflight + create，可在任意
          worktree（含 claude/*）里调用——先用共享 git 目录定位主工作区再建区，
          create/adopt 的“只在主工作区”约束不变。要求主工作区干净，否则请先用
          adopt 把在途改动搬进隔离区；通过后等同于 create。
create    建隔离区，写任务清单；只能在主工作区执行（start 会先切到主工作区）。
          迁移编号改为懒预留：默认不取号，真正要写迁移时用 reserve-migration；
          需要建区即取号可加 --reserve-migration。
adopt     把主工作区的在途改动（默认全部；可用 --path 限定）整体搬进新任务区，
          原始改动会保留为 stash 备份，主工作区恢复干净。路径按主工作区根解析；
          只能在主工作区执行。
reserve-migration
          为本任务懒预留一个迁移编号（幂等：已预留则原样返回）。只有真要新增
          migrations/ 文件时才调用，避免无数据库改动的任务消耗编号。
test      委托 release-test.sh，与发布候选使用同一份门禁。默认按改动范围分档：
          只改文档/CLI/测试（docs/、design/、tests/、tools/、*.md）跑 fast 档
          （语法检查 + 单元测试子集，写入独立 fast 日志，不写完整证据、不登记
          可复用验收树）；触及 app.py、app/static、migrations 等运行时代码跑
          full 档并记录完整证据。--full 强制完整档，--quick 仅语法检查（ship 用）。
          发布前门禁（publish/流水线）永远跑完整档，不受此分档影响。
          full 档要求任务已同步到最新 <main>，否则只对旧基线成立。
ship      开发方交付入口：sync 到最新 <main> → 快速门禁 → 把分支与 commit 登记到
          仓库外发布队列（status=shipped），然后立即返回。开发方不等待完整门禁；
          发布方会自己重跑完整门禁。要看完整门禁结果仍可单独 test --task <id>。
evidence  查看任务清单状态与最近一次完成证据。
flakes    汇总浏览器验收的 flake 台账（只读）：按步骤 + 结果计数并列出最近事件。
          台账由门禁在 Chromium 步骤失败重跑时写入；它只记录瞬时失败，不改变判定。
gate      只读检查任务是否 ready（门禁证据对应当前 HEAD 且已包含最新 main）；
          publish 内部使用同一判定。
reconcile 把本任务新增且与最新 main/其它任务冲突的迁移改名到下一个空号并提交。
hooks     刷新共享 git 目录里的入口隔离护栏（pre-commit + commit-msg）。
sync      先消除迁移编号碰撞，再变基到最新 origin/<main>（--offline 用本地基线）；
          变基后旧门禁证据失效，必须重新 test。
publish   只接受真正 ready 的任务：任务区干净、门禁证据对应当前 HEAD、HEAD 已包含
          最新 origin/<main>。随后委托 release-commit.sh --branch agent/<id>，在
          基于 origin/main 的临时 release worktree 里 cherry-pick、跑完整门禁、
          推送并发布。调用者工作区（含主工作区的在途改动）不参与发布。
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

# 完成证据：每次 test/publish 都把这棵树的 commit 与门禁结果落盘到共享目录。
# 它不在工作树内，不参与 commit，也不会让 require_clean 失败；人和其它 Agent
# 都可以据此判断“任务是否真的完成”，而不是听信一句自述。
task_evidence_path() { printf '%s/%s.verify.log' "$TASK_META_DIR" "$1"; }

# 合并任务清单字段（key=value），保留未列出的既有字段。
merge_task_meta() {
  local task=$1; shift
  local meta
  meta="$(task_meta_path "$task")"
  [[ -r "$meta" ]] || return 0
  python3 - "$meta" "$@" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    doc = json.load(handle)
for pair in sys.argv[2:]:
    key, _, value = pair.partition("=")
    if key:
        doc[key] = value
with open(path, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

# 快速门禁（--quick）只做语法检查，写入独立的 quick 日志，绝不覆盖完整证据、
# 也不更新完成判定；否则一次语法检查会让任务看起来“已通过门禁”。
task_quick_log_path() { printf '%s/%s.quick.log' "$TASK_META_DIR" "$1"; }

# 开发期快档（test 的默认分档之一）只跑语法 + 单元测试子集，写入独立的 fast 日志。
# 它同样**不**写 verify_result、不登记可复用验收树，因此发布前完整门禁照跑。
task_fast_log_path() { printf '%s/%s.fast.log' "$TASK_META_DIR" "$1"; }

# 读取任务清单里的 status 字段；清单缺失或没有 status 时输出空。
task_status() {
  local meta
  meta="$(task_meta_path "$1")"
  [[ -r "$meta" ]] || return 0
  python3 - "$meta" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = json.load(handle)
print(doc.get("status", ""))
PY
}

# 运行一次命令，把完整输出**追加**到指定证据文件，并在每次运行前附上可核验的元数据段。
# 返回被运行命令的退出码，调用方据此判定完成与否。
#
# 证据只追加、从不截断：一旦某次运行的 result: ok 已被记录（尤其任务已 landed），
# 之后任何重跑都只能新增一段，无法改写它——发布后重跑不会把已发布的验证结论洗掉。
# 文件头（task/branch）只在文件不存在时写入一次。
run_with_evidence() {
  local log=$1 task=$2 kind=$3 head=$4; shift 4
  local tmp status at result
  mkdir -p -- "$TASK_META_DIR"
  tmp="$(mktemp "${TMPDIR:-/tmp}/trosa-evidence.XXXXXX")"
  set +e
  "$@" >"$tmp" 2>&1
  status=$?
  set -e
  cat "$tmp"
  at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  result=ok
  [[ "$status" == 0 ]] || result=failed
  if [[ ! -f "$log" ]]; then
    {
      printf '# trosa task evidence\n'
      printf '# task: %s\n' "$task"
      printf '# branch: %s\n' "$(branch_of "$task")"
    } >>"$log"
  fi
  {
    printf '\n# --- run %s kind=%s commit=%s result=%s ---\n' "$at" "$kind" "$head" "$result"
    printf '# task: %s\n' "$task"
    printf '# at: %s\n' "$at"
    printf '# command: %s\n' "$*"
    cat "$tmp"
  } >>"$log"
  rm -f -- "$tmp"
  return "$status"
}

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
# 避免两个并行任务抢同一个号。调用方必须持有迁移预留锁（begin_migration_lock）。
next_migration_number() {
  trosa_next_migration_number "$MAIN_ROOT" "$TASK_META_DIR"
}

write_task_meta() {
  local task=$1 branch=$2 wt=$3 base=$4 owner=$5 goal=$6 scope=$7 reserved=$8
  command -v python3 >/dev/null 2>&1 || fail '找不到 python3，无法写任务清单'
  mkdir -p -- "$TASK_META_DIR"
  python3 - "$(task_meta_path "$task")" "$task" "$branch" "$wt" "$base" \
    "$owner" "$goal" "$scope" "$reserved" <<'PY'
import datetime
import json
import os
import sys

(path, task, branch, wt, base, owner, goal, scope, reserved) = sys.argv[1:10]
# adopt 会复用已有任务清单：保留已经积累的完成状态与证据，不要重置成未验证。
preserved = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as handle:
            preserved = json.load(handle)
    except (OSError, ValueError):
        preserved = {}
doc = {
    "task": task,
    "branch": branch,
    "path": wt,
    "base": base,
    "owner": owner,
    "goal": goal,
    "scope": scope,
    "reserved_migration": reserved,
    "status": preserved.get("status") or "active",
    "evidence": os.path.join(os.path.dirname(path), f"{task}.verify.log"),
    "created_at": datetime.datetime.now(
        datetime.timezone.utc
    ).astimezone().isoformat(timespec="seconds"),
}
for key in ("landed_commit", "landed_release", "landed_at",
            "verify_result", "verified_commit", "verified_at",
            "shipped_commit", "shipped_at"):
    if preserved.get(key):
        doc[key] = preserved[key]
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
import os
import subprocess
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
    ("synced_at", "最近同步"),
    ("verified_commit", "验证 commit"),
    ("verify_result", "最近门禁"),
    ("shipped_commit", "交付 commit"),
    ("shipped_at", "交付时间"),
    ("landed_commit", "落地 commit"),
    ("landed_release", "发布 release"),
    ("evidence", "证据文件"),
):
    value = doc.get(key)
    if value:
        print(f"  {label}：{value}")

head = ""
path = doc.get("path")
if path and os.path.isdir(path):
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        head = proc.stdout.strip()
verified = doc.get("verified_commit") or ""

status = doc.get("status") or "active"
if status == "landed":
    print(f"  完成判定：已发布（release={doc.get('landed_release') or '未知'}）")
elif status == "abandoned":
    print("  完成判定：已废弃")
elif status == "shipped":
    print("  完成判定：已交付到发布队列（等发布方重跑完整门禁后落地）")
elif doc.get("verify_result") == "ok" and head and verified != head:
    print("  完成判定：门禁证据已过期（对应当前 HEAD 之外的 commit），必须重新验证")
elif doc.get("verify_result") == "ok":
    print("  完成判定：开发完成并通过门禁，尚未发布")
elif doc.get("verify_result") == "failed":
    print("  完成判定：最近一次门禁未通过，不可发布")
elif doc.get("verify_result") == "stale":
    print("  完成判定：已同步到新基线，需重新验证后才能发布")
else:
    print("  完成判定：进行中（尚无验证证据）")
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

# ---------------------------------------------------------------------------
# 入口隔离：普通任务不得直接污染集成分支
# ---------------------------------------------------------------------------

# 版本化的 git 护栏（deploy/cloud/git-hooks/）复制到共享 git 目录。pre-commit 在
# dev/review 角色试图在集成分支提交时拒绝；commit-msg 兜底拒绝集成分支上的
# `[<id>]` 任务提交（未设置角色也不会误落）。人工作业与 release 角色不受影响。
# 每次调用 agent-worktree.sh 都刷新，保证护栏随脚本版本更新。
install_git_hooks() {
  local name src dest
  for name in pre-commit commit-msg; do
    src="$SCRIPT_DIR/git-hooks/$name"
    [[ -r "$src" ]] || continue
    [[ -d "$GIT_COMMON_DIR/hooks" ]] || mkdir -p "$GIT_COMMON_DIR/hooks" || return 0
    dest="$GIT_COMMON_DIR/hooks/$name"
    if [[ -f "$dest" ]] && ! grep -q 'sela/trosa 入口隔离' "$dest" 2>/dev/null; then
      printf '警告：已存在非本流程的 %s（%s），未覆盖。\n' "$name" "$dest" >&2
      continue
    fi
    if ! cmp -s "$src" "$dest"; then
      cp "$src" "$dest" && chmod 0755 "$dest" || return 0
    fi
  done
  return 0
}

# create/adopt 是“进入隔离区”的入口：只能在主工作区执行，不能在某个任务隔离区
# 里再建任务（那会让任务边界失去唯一归属）。
require_main_workspace() {
  local top
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$top" ]] || fail '当前目录不在任何 Git 工作树中'
  [[ "$top" == "$MAIN_ROOT" ]] \
    || fail "create/adopt 必须在主工作区（${MAIN_ROOT}）执行，不能在任务隔离区 $top 中创建任务"
}

# 任务同步/门禁校验用的基线：优先最新 origin/<main>，否则本地 <main>。
task_base_ref() {
  if git -C "$MAIN_ROOT" rev-parse --verify --quiet "refs/remotes/origin/$TARGET_BRANCH" >/dev/null 2>&1; then
    printf 'refs/remotes/origin/%s' "$TARGET_BRANCH"
  else
    printf '%s' "$TARGET_BRANCH"
  fi
}

head_contains() { git -C "$1" merge-base --is-ancestor "$2" HEAD; }

# 当前目录所在的任务 id（仅在 agent/<id> 隔离区里非空；主工作区与其它 worktree 为空）。
current_task_id() {
  local top branch
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$top" && "$top" != "$MAIN_ROOT" ]] || return 0
  branch="$(git -C "$top" symbolic-ref --short -q HEAD || printf 'detached')"
  task_of_branch "$branch"
}

# 本任务相对基线新增的迁移文件名（basename，含未跟踪文件）。体检用它把编号冲突
# 分成“与本任务相关 / 无关”：无关的既有冲突只提示、不阻断，避免每次开工都被
# 别人的 0041 撞号刷屏。
task_new_migration_basenames() {
  local wt=$1 base_ref mb
  base_ref="$(task_base_ref)"
  git -C "$wt" rev-parse --verify --quiet "$base_ref" >/dev/null 2>&1 || return 0
  mb="$(git -C "$wt" merge-base HEAD "$base_ref" 2>/dev/null || true)"
  [[ -n "$mb" ]] || mb="$base_ref"
  {
    git -C "$wt" diff --name-only --diff-filter=A "$mb" HEAD -- migrations/ 2>/dev/null
    git -C "$wt" ls-files --others --exclude-standard -- migrations/ 2>/dev/null
  } | sed '/^$/d' | sed 's#.*/##' | sort -u
}

# 读取任务清单里预留的迁移编号；未预留输出空。
task_reserved_number() {
  local meta
  meta="$(task_meta_path "$1")"
  [[ -r "$meta" ]] || return 0
  python3 - "$meta" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = json.load(handle)
value = str(doc.get("reserved_migration") or "")
print(value if value.isdigit() else "")
PY
}

# 本任务相对基线（含工作区未提交改动）的改动路径。分档只据此判断范围。
task_changed_paths() {
  local wt=$1 base_ref mb
  base_ref="$(task_base_ref)"
  mb="$(git -C "$wt" merge-base HEAD "$base_ref" 2>/dev/null || true)"
  [[ -n "$mb" ]] || mb="$base_ref"
  {
    git -C "$wt" diff --name-only "$mb" HEAD -- 2>/dev/null
    git -C "$wt" status --porcelain --untracked-files=all 2>/dev/null \
      | sed -e 's/^...//' -e 's/.* -> //'
  } | sed '/^$/d' | sort -u
}

# 开发期门禁分档（只影响开发方 test，不改变发布前完整门禁）：
#   fast  改动全部落在 docs/、design/、tests/、tools/ 或以 .md 结尾
#   full  其余情况（app.py、app/static、migrations 等运行时代码，空改动，T2 路径）
# 规则由 tools/release_tier.py 的 --dev-gate 给出（复用发布分级，但更保守、fail
# closed）；发布前的 release-commit.sh / 流水线从不读这个档位，始终完整档。
dev_gate_tier() {
  local wt=$1 tier_tool changed
  changed="$(task_changed_paths "$wt")"
  [[ -n "$changed" ]] || { printf 'full'; return 0; }
  tier_tool="$wt/tools/release_tier.py"
  [[ -r "$tier_tool" ]] || tier_tool="$MAIN_ROOT/tools/release_tier.py"
  printf '%s\n' "$changed" | python3 "$tier_tool" --dev-gate 2>/dev/null || printf 'full'
}

# 迁移编号校正：把本任务新增且与最新 main / 其它 worktree / 他人预留冲突的迁移
# 自动改名到下一个空号，并提交改名。无冲突时不做任何改动。
# 编号分配由 reconcile_migrations.py 在共享预留锁内从持久计数器取号，两个并发
# 任务不会拿到同一个号；已进入 main 或已记录在 applied ledger 的迁移绝不改名。
reconcile_task_migrations() {
  local task=$1 wt=$2 base out applied tool
  base="$(task_base_ref)"
  applied="$TASK_META_DIR/.applied-migrations"
  # Prefer the task tree's own copy when the task itself ships/updates the tool;
  # otherwise fall back to the integration tree. Same bootstrap rule as test.
  tool="$wt/tools/reconcile_migrations.py"
  [[ -r "$tool" ]] || tool="$MAIN_ROOT/tools/reconcile_migrations.py"
  [[ -r "$tool" ]] || fail "找不到迁移校正工具 $tool"
  out="$(python3 "$tool" \
    --task-dir "$wt" --target-ref "$base" \
    --meta-dir "$TASK_META_DIR" --task "$task" \
    --applied-ledger "$applied" --apply 2>&1)" || {
    printf '%s\n' "$out" >&2
    fail "任务 $task 迁移编号校正失败"
  }
  printf '%s\n' "$out"
  if [[ -n "$(git -C "$wt" status --porcelain)" ]]; then
    git -C "$wt" commit -q -m "[$task] renumber migrations to avoid parallel collision" \
      || fail "任务 $task 迁移改名提交失败"
    printf '已提交迁移改名：%s\n' "$(git -C "$wt" rev-parse --short HEAD)"
  fi
}

# 任务毕业门：发布只接受真正 ready 的任务。要求任务清单存在、未废弃、门禁证据
# 对应当前 HEAD、且 HEAD 已包含最新 main。返回 0 = ready。
check_task_ready() {
  local task=$1 wt=$2 meta head base status verify verified problems=0
  meta="$(task_meta_path "$task")"
  if [[ ! -r "$meta" ]]; then
    printf '任务 %s 缺少任务清单（未通过 create/adopt），不能发布。\n' "$task" >&2
    return 1
  fi
  head="$(git -C "$wt" rev-parse HEAD)"
  read -r status verify verified < <(python3 - "$meta" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = json.load(handle)
print(doc.get("status") or "active", doc.get("verify_result") or "", doc.get("verified_commit") or "")
PY
)
  if [[ "$status" == "abandoned" ]]; then
    printf '任务 %s 已废弃，不能发布。\n' "$task" >&2
    problems=1
  fi
  if [[ "$verify" != "ok" ]]; then
    printf '任务 %s 没有有效门禁证据（verify_result=%s）；先 sync 到最新 %s 再 test --task %s。\n' \
      "$task" "${verify:-无}" "$TARGET_BRANCH" "$task" >&2
    problems=1
  elif [[ "$verified" != "$head" ]]; then
    printf '任务 %s 的门禁证据对应 commit %s，与当前 HEAD %s 不一致；必须重新 test。\n' \
      "$task" "${verified:0:9}" "${head:0:9}" >&2
    problems=1
  fi
  base="$(task_base_ref)"
  if git -C "$wt" rev-parse --verify --quiet "$base" >/dev/null 2>&1; then
    if ! head_contains "$wt" "$base"; then
      printf '任务 %s 未基于最新 %s（HEAD 不包含 %s）；先 sync --task %s 并重新 test。\n' \
        "$task" "$TARGET_BRANCH" "$base" "$task" >&2
      problems=1
    fi
  else
    printf '警告：找不到基线 %s，跳过基线包含检查（先 fetch origin/%s）。\n' "$base" "$TARGET_BRANCH" >&2
  fi
  return "$problems"
}

# 门禁通过后登记“已验收树”，供 release 角色按对象身份复用（候选 tree hash +
# 门禁实现 + 外部输入 + 基线全部一致才命中）。只有工作树完全干净（含未跟踪的
# 非忽略文件）时，验收内容才等于 HEAD 的 git tree；否则标记为不可复用：
# verify_result 仍可为 ok，但 release 不会命中。
register_verified_tree() {
  local task=$1 wt=$2 gate_tree=$3
  local evid
  evid="$(task_evidence_path "$task")"
  if [[ -n "$(git -C "$wt" status --porcelain --untracked-files=all 2>/dev/null)" ]]; then
    printf '任务 %s 存在未提交改动：本次门禁仅对工作区成立，不作为可复用证据（未登记已验收树）。\n' "$task"
    merge_task_meta "$task" "verified_tree=" "reusable_tree=0"
    printf '# reusable_tree: 0（工作树有未提交改动）\n' >>"$evid" 2>/dev/null || true
    return 0
  fi
  local base_ref base identity key tree gate_impl external b
  base_ref="$(task_base_ref)"
  base="$(git -C "$wt" rev-parse --verify --quiet "$base_ref" 2>/dev/null || true)"
  if [[ -z "$base" ]]; then
    printf '任务 %s 基线 %s 无法解析：不登记可复用证据（fail closed）。\n' "$task" "$base_ref" >&2
    merge_task_meta "$task" "verified_tree=" "reusable_tree=0"
    printf '# reusable_tree: 0（基线无法解析：%s）\n' "$base_ref" >>"$evid" 2>/dev/null || true
    return 0
  fi
  identity="$(release_gate_identity "$wt" "$gate_tree" "$base" 2>/dev/null || true)"
  if [[ -z "$identity" ]]; then
    printf '任务 %s 无法计算门禁对象身份：不登记可复用证据（fail closed）。\n' "$task" >&2
    merge_task_meta "$task" "verified_tree=" "reusable_tree=0"
    printf '# reusable_tree: 0（无法计算门禁对象身份）\n' >>"$evid" 2>/dev/null || true
    return 0
  fi
  IFS=$'\t' read -r key tree gate_impl external b <<<"$identity"
  if ! release_gate_register "$GIT_COMMON_DIR" "$identity" "$task"; then
    printf '任务 %s 登记已验收树失败：不产生可复用证据。\n' "$task" >&2
    merge_task_meta "$task" "verified_tree=" "reusable_tree=0"
    printf '# reusable_tree: 0（登记已验收树失败）\n' >>"$evid" 2>/dev/null || true
    return 0
  fi
  merge_task_meta "$task" \
    "verified_tree=$tree" "reusable_tree=1" \
    "verified_tree_key=$key" "verified_gate_impl=$gate_impl" \
    "verified_external=$external" "verified_base=$b"
  {
    printf '# reusable_tree: 1\n'
    printf '# verified_tree: %s\n' "$tree"
    printf '# verified_base: %s\n' "$b"
    printf '# verified_gate_impl: %s\n' "$gate_impl"
    printf '# verified_external: %s\n' "$external"
  } >>"$evid" 2>/dev/null || true
  printf '已登记可复用门禁结论：tree=%s base=%s（release 候选命中时跳过全量门禁）\n' \
    "$tree" "${b:0:9}"
}

# 开发方入口（推荐）：合并 guard + status + preflight + create。可在任意 worktree
# 里调用（含桌面端会话建的 claude/* worktree）：先用共享 git 目录定位主工作区，再
# 切到主工作区建区，因此 create/adopt 的“只在主工作区”约束保持不变，Agent 也不
# 需要手动离开自己的目录。要求主工作区干净：在途改动属于新任务时请用 adopt（它
# 专门搬运在途改动并留 stash 备份），不要把脏改动留在主工作区里 create。
cmd_start() {
  local task="" arg idx=0
  local -a pass=("$@")
  while [[ $idx -lt ${#pass[@]} ]]; do
    arg="${pass[$idx]}"
    case "$arg" in
      --task) [[ $((idx+1)) -lt ${#pass[@]} ]] || fail '--task 需要一个 id'
              task="${pass[$((idx+1))]}"; idx=$((idx+2)); continue ;;
      *) idx=$((idx+1)) ;;
    esac
  done
  [[ -n "$task" ]] || fail 'start 需要 --task <id>'
  validate_task_id "$task"
  # 入口兼容任意 worktree：用共享 git 目录定位主工作区并切过去，之后仍走原有的
  # “只在主工作区建区”约束（require_main_workspace）。create/adopt 的对外约束不变。
  local origin_top
  origin_top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$origin_top" ]] || fail '当前目录不在任何 Git 工作树中'
  cd "$MAIN_ROOT"
  require_main_workspace
  local role
  role="$(trosa_agent_role)"
  if [[ "$origin_top" != "$MAIN_ROOT" ]]; then
    printf '（从 worktree %s 调用；已在主工作区 %s 建区）\n' "$origin_top" "$MAIN_ROOT"
  fi
  printf '角色：%s\n环境：主工作区（%s）\n' "$role" "$MAIN_ROOT"
  local dirty
  dirty="$(git -C "$MAIN_ROOT" status --porcelain --untracked-files=all)"
  if [[ -n "$dirty" ]]; then
    printf '主工作区有未提交改动：\n' >&2
    git -C "$MAIN_ROOT" status --short >&2
    fail "主工作区不干净，不能用 start 建新任务；若这些改动属于任务 ${task}，请改用 adopt --task ${task}（会把改动搬进隔离区并保留 stash 备份）"
  fi
  printf '主工作区干净。\n'
  # 并发体检：只提示，不阻断。已知的其它任务迁移号冲突等不应挡住建区，
  # create 之前的硬性检查仍由 create 执行。
  local pf=0
  cmd_preflight || pf=$?
  if [[ "$pf" != 0 ]]; then
    printf '提醒：并发体检有未通过项（见上）。若与本任务无关可继续。\n' >&2
  fi
  cmd_create ${pass[@]+"${pass[@]}"}
}

# 懒预留迁移号：只有真要新增 migrations/ 文件时才取号，避免无数据库改动的任务
# 消耗单调计数器。幂等：已预留则原样返回，不重复占用编号。
cmd_reserve_migration() {
  local task=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      *) fail "reserve-migration 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'reserve-migration 需要 --task <id>'
  validate_task_id "$task"
  local meta
  meta="$(task_meta_path "$task")"
  [[ -r "$meta" ]] || fail "任务 $task 尚无任务清单（先 start/create/adopt 建区）"
  local existing
  existing="$(python3 - "$meta" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        doc = json.load(handle)
except (OSError, ValueError):
    doc = {}
value = str(doc.get("reserved_migration") or "")
print(value if value.isdigit() else "")
PY
)"
  if [[ -n "$existing" ]]; then
    printf '任务 %s 已预留迁移编号：%s\n' "$task" "$existing"
    return 0
  fi
  local reserved
  begin_migration_lock
  reserved="$(next_migration_number)"
  merge_task_meta "$task" "reserved_migration=$reserved"
  release_migration_lock
  printf '任务 %s 预留迁移编号：%s\n' "$task" "$reserved"
  printf '建议文件名：migrations/%s_<说明>.sql\n' "$reserved"
}

cmd_create() {
  # 迁移编号改为懒预留：默认不取号，避免无数据库改动的任务消耗单调计数器；
  # 真要写迁移时用 reserve-migration，或建区即取号加 --reserve-migration。
  # --no-reserve-migration 保留为兼容参数（与默认行为一致）。
  local task="" base="$TARGET_BRANCH" fetch_base=0 owner="" goal="" scope="" reserve=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --base) [[ $# -ge 2 ]] || fail '--base 需要一个 ref'; base=$2; shift 2 ;;
      --fetch-base) fetch_base=1; shift ;;
      --owner) [[ $# -ge 2 ]] || fail '--owner 需要一个值'; owner=$2; shift 2 ;;
      --goal) [[ $# -ge 2 ]] || fail '--goal 需要文字'; goal=$2; shift 2 ;;
      --scope) [[ $# -ge 2 ]] || fail '--scope 需要文字'; scope=$2; shift 2 ;;
      --reserve-migration) reserve=1; shift ;;
      --no-reserve-migration) reserve=0; shift ;;
      *) fail "create 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'create 需要 --task <id>'
  validate_task_id "$task"
  require_main_workspace
  git -C "$MAIN_ROOT" worktree prune
  git -C "$MAIN_ROOT" rev-parse --verify --quiet "refs/heads/$(branch_of "$task")" >/dev/null \
    && fail "分支 $(branch_of "$task") 已存在（换 id，或用 sync/remove 处理旧任务）"
  [[ -e "$(path_of "$task")" ]] && fail "目录 $(path_of "$task") 已存在"
  if [[ "$fetch_base" == 1 ]]; then
    git -C "$MAIN_ROOT" fetch --quiet origin "$TARGET_BRANCH" \
      || fail "fetch origin/$TARGET_BRANCH 失败（网络不可用时去掉 --fetch-base，用本地 ${TARGET_BRANCH}）"
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
    begin_migration_lock
    reserved="$(next_migration_number)"
    write_task_meta "$task" "$(branch_of "$task")" "$wt" "$base" "$owner" "$goal" "$scope" "$reserved"
    release_migration_lock
  else
    write_task_meta "$task" "$(branch_of "$task")" "$wt" "$base" "$owner" "$goal" "$scope" "$reserved"
  fi
  printf '\n任务隔离区已就绪：\n  目录：%s\n  分支：%s（基线 %s）\n' "$wt" "$(branch_of "$task")" "$base"
  print_task_meta "$task"
  if [[ -n "$reserved" ]]; then
    printf '  预留迁移编号：%s（无数据库改动时忽略）\n' "$reserved"
  else
    printf '  迁移编号：未预留（要写 migrations/ 时运行 reserve-migration --task %s）\n' "$task"
  fi
  printf '下一步：在该目录改代码、commit 到本任务分支；交付用 ship，看完整门禁用 test。\n'
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
  local task="" quick=0 force_full=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --quick) quick=1; shift ;;
      --full) force_full=1; shift ;;
      *) fail "test 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'test 需要 --task <id>'
  validate_task_id "$task"
  local wt args=() gate gate_tree tier=full
  wt="$(find_task_path "$task")"
  [[ -d "$wt" ]] || fail "隔离区目录缺失：$wt"
  # 开发期分档：只改文档/CLI/测试走快档（语法 + 单元测试子集），触及 app.py、
  # app/static、migrations 等运行时代码走完整档；--full 可强制完整档。这只是
  # 本地反馈的快慢，发布前门禁（publish/流水线）永远跑完整档，绝不因此放宽。
  if [[ "$quick" != 1 && "$force_full" != 1 ]]; then
    tier="$(dev_gate_tier "$wt")"
  fi
  # 完整门禁必须基于最新 main：否则“绿”只对旧基线成立。快速与快档不受限。
  if [[ "$quick" != 1 ]]; then
    if [[ "$tier" == full ]]; then
      local base_ref
      base_ref="$(task_base_ref)"
      if git -C "$wt" rev-parse --verify --quiet "$base_ref" >/dev/null 2>&1; then
        head_contains "$wt" "$base_ref" \
          || fail "任务 $task 尚未同步到最新 ${TARGET_BRANCH}（HEAD 不包含 ${base_ref}）；先 sync --task $task 再 test"
      else
        printf '警告：找不到基线 %s，跳过基线包含检查（先 fetch origin/%s）。\n' "$base_ref" "$TARGET_BRANCH" >&2
      fi
    fi
  fi
  # If the task branch already contains the gate, test that exact version;
  # otherwise use the repository's committed gate while bootstrapping it.
  gate="$wt/deploy/cloud/release-test.sh"
  [[ -r "$gate" ]] || gate="$MAIN_ROOT/deploy/cloud/release-test.sh"
  [[ -r "$gate" ]] || fail "找不到发布门禁 $MAIN_ROOT/deploy/cloud/release-test.sh"
  # 实际执行门禁的那棵树（登记已验收树时锚定门禁实现）：通常是本任务树，
  # 引导期回退到主仓时则是主仓。
  gate_tree="$(git -C "$(dirname "$gate")" rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$gate_tree" ]] || gate_tree="$MAIN_ROOT"
  args=(--dir "$wt")
  if [[ "$quick" == 1 ]]; then
    args+=(--quick)
  elif [[ "$tier" == fast ]]; then
    args+=(--fast)
  fi
  local head iso log kind
  head="$(git -C "$wt" rev-parse HEAD)"
  if [[ "$quick" == 1 ]]; then
    log="$(task_quick_log_path "$task")"; kind="test-quick"
  elif [[ "$tier" == fast ]]; then
    log="$(task_fast_log_path "$task")"; kind="test-fast"
  else
    log="$(task_evidence_path "$task")"; kind="test-full"
  fi
  iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # 已 landed 的任务：本次运行只追加证据，绝不改写已发布的验证结论/状态。
  local landed=0
  if [[ "$(task_status "$task")" == landed ]]; then
    landed=1
  fi
  if run_with_evidence "$log" "$task" "$kind" "$head" bash "$gate" ${args[@]+"${args[@]}"}; then
    if [[ "$quick" == 1 ]]; then
      printf '任务 %s 快速门禁通过（仅语法检查，不作为完成证据）。\n' "$task"
      printf '完整门禁：test --task %s（不带 --quick）。快速日志：%s\n' "$task" "$log"
    elif [[ "$tier" == fast ]]; then
      merge_task_meta "$task" \
        "fast_result=ok" "fast_commit=$head" "fast_at=$iso" \
        "fast_evidence=$log" "fast_tier=fast" "reusable_tree=0"
      printf '任务 %s 开发快档通过（分档：fast；语法检查 + 单元测试子集，跳过 PostgreSQL/Chromium/扩展）。\n' "$task"
      printf '快档不写完整门禁证据、不登记可复用验收树；发布前仍会跑完整档，因此没有放宽任何门禁。\n'
      printf '完整档：test --task %s --full。快档日志：%s（commit %s）\n' "$task" "$log" "${head:0:9}"
    else
      if [[ "$landed" == 1 ]]; then
        printf '任务 %s 已是 landed；本次只追加证据，不改写已发布的验证结论。\n' "$task"
      else
        merge_task_meta "$task" \
          "verify_result=ok" "verified_commit=$head" "verified_at=$iso" "evidence=$log" \
          "gate_tier=full"
        register_verified_tree "$task" "$wt" "$gate_tree"
        printf '任务 %s 验证完成（分档：full；门禁实现：release-test.sh）。\n' "$task"
      fi
      printf '证据：%s（commit %s）\n' "$log" "${head:0:9}"
    fi
  else
    local status=$?
    if [[ "$quick" == 1 ]]; then
      fail "任务 $task 快速门禁失败（仅语法，未改动完成判定）：$log"
    fi
    if [[ "$tier" == fast ]]; then
      merge_task_meta "$task" \
        "fast_result=failed" "fast_commit=$head" "fast_at=$iso" \
        "fast_evidence=$log" "fast_tier=fast" "reusable_tree=0"
      fail "任务 $task 开发快档失败（证据：${log}，退出码 ${status}）；可用 --full 跑完整档定位"
    fi
    if [[ "$landed" == 1 ]]; then
      fail "任务 $task 已是 landed 但本次重跑门禁未通过；证据已追加，不改写已发布结论（证据：$log，退出码 $status）"
    fi
    merge_task_meta "$task" \
      "verify_result=failed" "verified_commit=$head" "verified_at=$iso" "evidence=$log" \
      "reusable_tree=0"
    fail "任务 $task 门禁未通过，不可发布（证据：${log}，退出码 ${status}）"
  fi
}

# 开发方交付入口：sync 到最新 <main> → 快速门禁 → 把分支与 commit 登记到仓库外
# 发布队列（status=shipped），然后立即返回。开发方不等待完整门禁；发布方会自己
# 重跑一遍完整门禁（开发方的 verify.log 不作为判定依据）。要看完整门禁结果仍可
# 单独 test --task <id>。故意不在此处 git push：发布形态（队列/远端）由发布侧定，
# 开发会话不应做对外可见操作。
cmd_ship() {
  local task="" offline=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --offline) offline=1; shift ;;
      *) fail "ship 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'ship 需要 --task <id>'
  validate_task_id "$task"
  local wt
  wt="$(find_task_path "$task")"
  [[ -d "$wt" ]] || fail "隔离区目录缺失：$wt"
  local -a sync_args=(--task "$task")
  if [[ "$offline" == 1 ]]; then sync_args+=(--offline); fi
  cmd_sync ${sync_args[@]+"${sync_args[@]}"}
  cmd_test --task "$task" --quick
  local head iso queue
  head="$(git -C "$wt" rev-parse HEAD)"
  iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  merge_task_meta "$task" "status=shipped" "shipped_commit=$head" "shipped_at=$iso"
  mkdir -p -- "$TASK_META_DIR"
  queue="$TASK_META_DIR/.ship-queue"
  printf '%s\t%s\t%s\t%s\n' "$iso" "$task" "$(branch_of "$task")" "$head" >> "$queue"
  printf '\n任务 %s 已交付到发布队列。\n  分支：%s\n  交付 commit：%s\n  队列：%s\n' \
    "$task" "$(branch_of "$task")" "${head:0:9}" "$queue"
  printf '发布方会自己重跑完整门禁后发布；开发方无需在此等待。\n'
  printf '要看完整门禁结果：test --task %s（不带 --quick）。\n' "$task"
}

cmd_evidence() {
  local task=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      *) fail "evidence 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'evidence 需要 --task <id>'
  validate_task_id "$task"
  print_task_meta "$task"
  local log
  log="$(task_evidence_path "$task")"
  if [[ -r "$log" ]]; then
    printf '\n最近一次完成证据（%s）：\n' "$log"
    cat "$log"
  else
    printf '\n尚无完成证据：先运行 test --task %s。\n' "$task"
  fi
}

# 汇总浏览器验收的 flake 台账（只读）。台账由 release-test.sh 在浏览器步骤失败重跑
# 时写入；这里只做聚合展示，不改变任何门禁结论。
cmd_flakes() {
  local limit=20
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --limit) [[ $# -ge 2 ]] || fail '--limit 需要一个数字'; limit=$2; shift 2 ;;
      *) fail "flakes 未知参数：$1" ;;
    esac
  done
  local ledger
  ledger="$(release_gate_flake_ledger_path "$MAIN_ROOT")" || fail '无法定位 flake 台账'
  printf 'flake 台账：%s\n' "$ledger"
  if [[ ! -s "$ledger" ]]; then
    printf '尚无 flake 事件。\n'
    return 0
  fi
  printf '\n按「步骤 + 结果」汇总（retrying 为未决的第一次失败，ok/failed 为终态）：\n'
  awk -F'\t' '{ c[$2" | "$5]++ } END { for (k in c) printf "%6d  %s\n", c[k], k }' "$ledger" \
    | LC_ALL=C sort -k2
  printf '\n最近 %s 条事件（时间 / 步骤 / 首次退出码 / 结果）：\n' "$limit"
  tail -n "$limit" "$ledger" | awk -F'\t' '{ printf "%s  %s  exit=%s  %s\n", $1, $2, $4, $5 }'
  printf '\n提示：这是浏览器验收的瞬时失败记录，不等于代码缺陷。若某步骤反复出现，\n'
  printf '应作为真实缺陷排查，而不是依赖重跑。\n'
}

cmd_gate() {
  local task=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      *) fail "gate 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'gate 需要 --task <id>'
  validate_task_id "$task"
  local wt
  wt="$(find_task_path "$task")"
  [[ -d "$wt" ]] || fail "隔离区目录缺失：$wt"
  printf '任务 %s 发布条件检查（HEAD %s）\n' "$task" "$(git -C "$wt" rev-parse --short HEAD)"
  if check_task_ready "$task" "$wt"; then
    printf '结论：ready（可作为发布输入）\n'
  else
    printf '结论：not_ready（见上）\n' >&2
    return 1
  fi
}

cmd_reconcile() {
  local task=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      *) fail "reconcile 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'reconcile 需要 --task <id>'
  validate_task_id "$task"
  local wt
  wt="$(find_task_path "$task")"
  require_clean "$wt" "任务 $task"
  reconcile_task_migrations "$task" "$wt"
}

cmd_hooks() {
  local name
  printf '入口隔离护栏目录：%s/hooks\n' "$GIT_COMMON_DIR"
  for name in pre-commit commit-msg; do
    [[ -r "$SCRIPT_DIR/git-hooks/$name" ]] || continue
    if [[ -x "$GIT_COMMON_DIR/hooks/$name" ]]; then
      printf '  %s：已安装\n' "$name"
    else
      printf '  %s：缺失（源 %s/git-hooks/%s）\n' "$name" "$SCRIPT_DIR" "$name"
    fi
  done
}

cmd_sync() {
  local task="" offline=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) [[ $# -ge 2 ]] || fail '--task 需要一个 id'; task=$2; shift 2 ;;
      --fetch-base) shift ;;  # 兼容旧参数：sync 现在默认拉取最新基线
      --offline) offline=1; shift ;;
      *) fail "sync 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'sync 需要 --task <id>'
  validate_task_id "$task"
  local wt base iso
  wt="$(find_task_path "$task")"
  require_clean "$wt" "任务 $task"
  if [[ "$offline" != 1 ]]; then
    git -C "$MAIN_ROOT" fetch --quiet origin "$TARGET_BRANCH" \
      || fail "fetch origin/$TARGET_BRANCH 失败（离线时用 --offline 基于本地 $TARGET_BRANCH 同步）"
  fi
  base="$(task_base_ref)"
  # 先消除并行迁移编号碰撞，再变基；改名会作为本任务的一个 commit 保留。
  reconcile_task_migrations "$task" "$wt"
  require_clean "$wt" "任务 ${task}（迁移改名未提交）"
  if git -C "$wt" rebase "$base"; then
    iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # 变基后旧的门禁证据不再对应当前 HEAD，必须重跑完整门禁才能发布。
    merge_task_meta "$task" \
      "verify_result=stale" "verified_commit=" "verified_at=" "synced_at=$iso"
    printf '任务 %s 已变基到 %s；原验证证据已失效，发布前必须重新 test --task %s。\n' \
      "$task" "$base" "$task"
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
  # 发布角色边界：dev/review 角色可以开发与验证，但不能改动 production。
  trosa_require_release_role || fail '当前角色没有发布权限（publish 只属于 release 角色）'
  local wt branch head
  wt="$(find_task_path "$task")"
  branch="$(branch_of "$task")"
  require_clean "$wt" "任务 ${task}（改动先 commit 到 ${branch}）"
  # 发布只接受真正 ready 的任务：基线要最新，门禁证据要对应当前 HEAD。
  git -C "$MAIN_ROOT" fetch --quiet origin "$TARGET_BRANCH" \
    || fail "publish 需要最新 origin/$TARGET_BRANCH 才能判定任务是否 ready；fetch 失败"
  reconcile_task_migrations "$task" "$wt"
  require_clean "$wt" "任务 ${task}（迁移改名未提交）"
  check_task_ready "$task" "$wt" \
    || fail "任务 $task 未达到发布条件；先 sync --task $task 再 test --task $task"
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
  local log iso release landed
  log="$(task_evidence_path "$task")"
  iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if run_with_evidence "$log" "$task" publish "$head" \
      bash "$MAIN_ROOT/deploy/cloud/auto-publish.sh" --branch "$branch"; then
    release="$(grep -o 'release=[^ ]*' "$log" | tail -n 1 | cut -d= -f2 || true)"
    landed="$(grep -o 'RELEASE_COMMIT_SUCCESS commit=[0-9a-f]*' "$log" | tail -n 1 | sed 's/.*commit=//' || true)"
    merge_task_meta "$task" \
      "status=landed" \
      "landed_commit=${landed:-$head}" \
      "landed_release=${release}" \
      "landed_at=$iso" \
      "verify_result=ok" \
      "verified_commit=$head" \
      "verified_at=$iso" \
      "evidence=$log"
    printf '\n任务 %s 已发布。\n' "$task"
    printf '完成证据：%s（发布 release=%s）\n' "$log" "${release:-未知}"
  else
    merge_task_meta "$task" \
      "status=active" \
      "verify_result=failed" \
      "verified_commit=$head" \
      "verified_at=$iso" \
      "evidence=$log"
    fail "任务 $task 发布未完成（证据：${log}）；未标记为完成"
  fi
}

cmd_status() {
  local top branch task dirty_count role
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$top" ]] || fail '当前目录不在任何 Git 工作树中'
  branch="$(git -C "$top" symbolic-ref --short -q HEAD || printf 'detached')"
  task="$(task_of_branch "$branch")"
  role="$(trosa_agent_role)"
  printf '角色：%s（dev/review 不能发布；只有 release 能发布）\n' "$role"
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
    case "$role" in
      dev|development|review|readonly|read-only)
        printf '  注意：你的角色是 %s，guard 会拒绝在主工作区开始开发任务；请先 create/adopt。\n' "$role"
        ;;
    esac
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

# 开发任务开始前的入口闸门。dev/review 角色只能从 agent/<id> 任务隔离区开始工作；
# 在主工作区（或任何非任务目录）尝试开始开发会被明确拒绝，要求在写任何文件之前
# 先 create/adopt。release 角色与未设置角色（人工集成）不受影响，只读的 status
# 也不受影响。命令本身只读，不修改工作区、不申请编号。
cmd_guard() {
  local top branch task role
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$top" ]] || fail '当前目录不在任何 Git 工作树中'
  role="$(trosa_agent_role)"
  branch="$(git -C "$top" symbolic-ref --short -q HEAD || printf 'detached')"
  task="$(task_of_branch "$branch")"
  case "$role" in
    dev|development|review|readonly|read-only)
      if [[ "$top" == "$MAIN_ROOT" ]]; then
        fail "开发/审查角色（${role}）不得在主工作区开始任务：主工作区只做集成/验收/发布。请在 $MAIN_ROOT 运行 create（已有在途改动则用 adopt）进入 agent/<id> 隔离区后再改代码，避免先把主工作区写脏。"
      fi
      if [[ -z "$task" ]]; then
        case "$branch" in
          claude/*)
            # 桌面端会话为每个会话建一个 claude/* worktree。它不是一个任务隔离区，
            # 但也不必离开：在当前目录直接 start 就会在主工作区建好 agent/<id> 区。
            fail "检测到桌面端会话 worktree（分支 ${branch}）。无需切到主工作区：直接在当前目录运行
  deploy/cloud/agent-worktree.sh start --task <id> --owner <name> --goal \"...\" --scope \"...\"
即可建好 agent/<id> 任务隔离区，再进入该目录工作。"
            ;;
          *)
            fail "开发/审查角色（${role}）当前不在 agent/<id> 任务隔离区（目录 ${top}，分支 ${branch}）。请回到主工作区 $MAIN_ROOT 用 create/adopt 建立任务，或在当前 worktree 直接 start --task <id>。"
            ;;
        esac
      fi
      printf 'guard：可以开始任务\n  角色：%s\n  任务：%s\n  目录：%s\n  分支：%s\n' \
        "$role" "$task" "$top" "$branch"
      ;;
    *)
      printf 'guard：可以继续\n  角色：%s（未限制角色，集成/发布场景不受影响）\n  目录：%s\n  分支：%s\n' \
        "$role" "$top" "$branch"
      ;;
  esac
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
  # 体检分“与本任务相关 / 无关”：只有本任务新增迁移撞号、或本任务预留号与别人
  # 重复时才阻断；别人的既有冲突降级为一行提示，不阻断、不每次刷屏。
  local cur_task new_migrations conflicts related="" unrelated="" num names nm is_related
  cur_task="$(current_task_id)"
  new_migrations=""
  if [[ -n "$cur_task" ]]; then
    local cur_wt
    cur_wt="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    new_migrations="$(task_new_migration_basenames "$cur_wt")"
  fi
  conflicts="$(all_migration_basenames | python3 -c '
import collections, sys

by_number = collections.defaultdict(set)
for name in sys.stdin:
    name = name.strip()
    if name:
        by_number[name[:4]].add(name)
for number in sorted(by_number):
    if len(by_number[number]) > 1:
        print(number + "\t" + ",".join(sorted(by_number[number])))
')"
  if [[ -n "$conflicts" ]]; then
    while IFS=$'\t' read -r num names; do
      [[ -n "$num" ]] || continue
      is_related=0
      if [[ -n "$new_migrations" ]]; then
        while IFS= read -r nm; do
          [[ -n "$nm" ]] || continue
          case ",$names," in *",$nm,"*) is_related=1 ;; esac
        done <<<"$new_migrations"
      fi
      if [[ "$is_related" == 1 ]]; then
        related+="  ${num}: ${names//,/, }"$'\n'
      else
        unrelated+="  ${num}: ${names//,/, }"$'\n'
      fi
    done <<<"$conflicts"
  fi
  if [[ -n "$related" ]]; then
    printf '  与本任务相关的编号冲突（本任务新增迁移与其它树撞号，合并前必须改名）：\n%s' "$related"
    problems=$((problems + 1))
  fi
  if [[ -n "$unrelated" ]]; then
    local flat
    flat="$(printf '%s' "$unrelated" | tr '\n' ';' | sed 's/  */ /g; s/^ //; s/;[[:space:]]*$//; s/; */；/g')"
    printf '  提示：与本任务无关的既有迁移编号冲突（不阻断本任务，建议单列任务处理）：%s\n' "$flat"
  fi
  if [[ -z "$related" && -z "$unrelated" ]]; then
    printf '  未发现跨任务编号冲突（每棵树内仍需通过 check_migrations.py）。\n'
  fi

  local dup_reserved cur_reserved rel_dup="" unrel_dup="" dup
  dup_reserved="$(reserved_migration_numbers | sort | uniq -d)"
  if [[ -n "$dup_reserved" ]]; then
    cur_reserved=""
    [[ -n "$cur_task" ]] && cur_reserved="$(task_reserved_number "$cur_task")"
    while IFS= read -r dup; do
      [[ -n "$dup" ]] || continue
      if [[ -n "$cur_reserved" && "$dup" == "$cur_reserved" ]]; then
        rel_dup+=" $dup"
      else
        unrel_dup+=" $dup"
      fi
    done <<<"$dup_reserved"
    if [[ -n "${rel_dup// }" ]]; then
      printf '  多个活跃任务预留了与本任务相同的编号（合并前必须错开）：%s\n' "$rel_dup"
      problems=$((problems + 1))
    fi
    if [[ -n "${unrel_dup// }" ]]; then
      printf '  提示：与本任务无关的预留编号重复（不阻断本任务）：%s\n' "$unrel_dup"
    fi
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
  require_main_workspace

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
  begin_migration_lock
  reserved="$(next_migration_number)"
  write_task_meta "$task" "$(branch_of "$task")" "$wt" "$TARGET_BRANCH" "$owner" "$goal" "$scope" "$reserved"
  release_migration_lock

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
    require_clean "$wt" "任务 ${task}（未提交改动会丢失；确认丢弃请加 --force）"
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
# Every session refreshes the shared entry guard, so a dev/review role cannot
# commit on the integration branch even before it runs create/adopt.
install_git_hooks
case "$command" in
  status) cmd_status "$@" ;;
  guard) cmd_guard "$@" ;;
  preflight) cmd_preflight "$@" ;;
  start) cmd_start "$@" ;;
  create) cmd_create "$@" ;;
  adopt) cmd_adopt "$@" ;;
  reserve-migration) cmd_reserve_migration "$@" ;;
  list) cmd_list "$@" ;;
  test) cmd_test "$@" ;;
  ship) cmd_ship "$@" ;;
  evidence) cmd_evidence "$@" ;;
  flakes) cmd_flakes "$@" ;;
  gate) cmd_gate "$@" ;;
  reconcile) cmd_reconcile "$@" ;;
  hooks) cmd_hooks "$@" ;;
  sync) cmd_sync "$@" ;;
  publish) cmd_publish "$@" ;;
  remove) cmd_remove "$@" ;;
  --help|-h|help) usage ;;
  *) fail "未知命令：${command}（用 --help 查看）" ;;
esac
