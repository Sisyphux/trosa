#!/usr/bin/env bash
# 常驻发布流水线（capability-based release pipeline）——只消费交付队列。
#
# 定位
# ====
# 开发方（propose 能力）只能改代码、commit、sync、跑快速检查，并用
# `agent-worktree.sh ship` 把分支写进交付队列。发布方是一个持有发布凭据的常驻
# 进程（release 能力），它自己重新计算一切，不信任开发方交来的结论：
#
#   1. 从队列取出待交付的 commit；核对它与任务清单里登记的 shipped_commit 一致。
#   2. 按候选 diff 相对 origin/main 计算风险级别（tools/release_tier.py），
#      分级规则属于流水线一侧，开发方无法把它标成低风险。
#   3. 级别不再由开发方声明，也不再读开发方写下的 verify.log。
#
# 队列与游标
# ==========
#   队列  $TASK_META_DIR/.ship-queue      （agent-worktree.sh ship 追加，TSV）
#         列：<UTC 时间>\t<task>\t<branch>\t<commit>
#   游标  $TASK_META_DIR/.pipeline-state  （本脚本追加，记录每个 commit 的处置）
#         列：<UTC 时间>\t<task>\t<commit>\t<结果>\t<release>
#
# 安全边界
# ========
# 真正的发布能力来自仓库外的发布配置与云 AK（~/.config/trosa/workbench.env 与
# ~/.workbench/config.json）；开发会话读不到它们，因此拿不到发布能力。本脚本
# 本身不存放任何凭据。
#
# 用法
# ====
#   deploy/cloud/release-pipeline.sh classify --task <id>
#   deploy/cloud/release-pipeline.sh status
#   deploy/cloud/release-pipeline.sh run [--dry-run] [--publish] [--task <id>] [--limit N] [--offline]
#
#   run 默认是 dry-run：完整走一遍 cherry-pick → 全量门禁 → 数据库预检，
#   但不推送、不发布。真正发布必须显式给 --publish（阶段4 只支持 T1，且要求
#   release 能力；T0/T2 不自动发布）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

GIT_COMMON_DIR="$(cd "$SCRIPT_DIR" && git rev-parse --git-common-dir 2>/dev/null)" \
  || { printf '无法解析 git common dir\n' >&2; exit 1; }
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$(cd "$SCRIPT_DIR" && cd "$GIT_COMMON_DIR" && pwd)" ;;
esac
MAIN_ROOT="$(cd "$GIT_COMMON_DIR/.." && pwd)"
TARGET_BRANCH="${TRADE_OS_AUTO_PUBLISH_BRANCH:-main}"
TASK_META_DIR="$GIT_COMMON_DIR/trosa-tasks"
BASE_REF="refs/remotes/origin/$TARGET_BRANCH"
QUEUE_PATH="$TASK_META_DIR/.ship-queue"
STATE_PATH="$TASK_META_DIR/.pipeline-state"
PIPELINE_LOCK="$TASK_META_DIR/.pipeline.lock"
TIER_TOOL="$PIPELINE_ROOT/tools/release_tier.py"
# 发布入口必须来自流水线自己的权威检出（与 release-test.sh 门禁同一原则：
# 候选不可自证合格）。release-commit.sh 自身还有“发布基础设施版本门”，会从
# origin/main 的干净 worktree 重新执行同一份正式发布逻辑。
PUBLISH_ENTRY="$SCRIPT_DIR/release-commit.sh"

# shellcheck source=release-env.sh
. "$SCRIPT_DIR/release-env.sh"
# shellcheck source=lib-release-lock.sh
. "$SCRIPT_DIR/lib-release-lock.sh"

fail() { printf '流水线：%s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

usage() {
  cat <<'EOF'
用法：
  deploy/cloud/release-pipeline.sh classify --task <id> [--offline]
  deploy/cloud/release-pipeline.sh status
  deploy/cloud/release-pipeline.sh run [--dry-run] [--publish] [--task <id>] [--limit N] [--offline]

  classify   只读：按任务已交付的 commit 相对 origin/main 的改动计算 T0/T1/T2。
  status     只读：打印交付队列与已处置结果。
  run        消费交付队列。默认 dry-run（cherry-pick + 全量门禁 + 数据库预检，
             不推送、不发布）；--publish 才真正发布，且只对 T1 生效。
             T2 一律停在 awaiting-approval（等 approve，阶段5 实现）；
             T0 停 在 merge-only（合入 main 不部署，阶段4 尚未启用）。

环境：
  TRADE_OS_AUTO_PUBLISH_BRANCH  目标分支（默认 main）
  TRADE_OS_WORKBENCH_ENV        发布配置显式路径（默认仓库外 ~/.config/trosa/workbench.env）
EOF
}

# ---------------------------------------------------------------------------
# 队列 / 游标
# ---------------------------------------------------------------------------
queue_lines() {
  [[ -r "$QUEUE_PATH" ]] || return 0
  grep -v '^[[:space:]]*$' "$QUEUE_PATH" | grep -v '^[[:space:]]*#' || true
}

state_commits() {
  [[ -r "$STATE_PATH" ]] || return 0
  awk -F'\t' 'NF >= 4 { print $3 }' "$STATE_PATH"
}

record_state() {
  local task=$1 commit=$2 outcome=$3 release=${4:-}
  mkdir -p "$TASK_META_DIR"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(utc_now)" "$task" "$commit" "$outcome" "$release" >> "$STATE_PATH"
}

# 与 agent-worktree.sh 的 write_task_meta/merge_task_meta 语义一致（字符串写入）。
# 这里无法 source agent-worktree.sh（它顶层即会分发子命令），因此本地实现一份
# 极小的合并；它只写任务清单，不参与任何门禁判定。
merge_task_meta() {
  local task=$1; shift
  local meta="$TASK_META_DIR/$task.json"
  [[ -f "$meta" ]] || return 0
  python3 - "$meta" "$@" <<'PY'
import json
import os
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
except (OSError, ValueError):
    doc = {}
for item in sys.argv[2:]:
    if "=" not in item:
        continue
    key, value = item.split("=", 1)
    doc[key] = value
tmp = f"{path}.{os.getpid()}.tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
os.replace(tmp, path)
PY
}

meta_field() {
  local task=$1 field=$2
  local meta="$TASK_META_DIR/$task.json"
  [[ -r "$meta" ]] || return 0
  python3 - "$meta" "$field" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = json.load(handle)
print(doc.get(sys.argv[2]) or "")
PY
}

# ---------------------------------------------------------------------------
# 候选与分级
# ---------------------------------------------------------------------------
candidate_base() {
  git -C "$MAIN_ROOT" rev-parse --verify --quiet "$BASE_REF" 2>/dev/null
}

fetch_base() {
  local offline=$1
  [[ "$offline" == 1 ]] && return 0
  git -C "$MAIN_ROOT" fetch --quiet origin "$TARGET_BRANCH" || true
}

# 候选改动 = merge-base(origin/main, commit)..commit 的 name-only 列表。
candidate_paths() {
  local commit=$1
  local base merge
  base="$(candidate_base)" || return 1
  [[ -n "$base" ]] || return 1
  if ! merge="$(git -C "$MAIN_ROOT" merge-base "$base" "$commit" 2>/dev/null)"; then
    return 1
  fi
  git -C "$MAIN_ROOT" diff --name-only "$merge" "$commit"
}

# 打印级别；理由写到 stderr。
tier_of_commit() {
  local commit=$1
  local paths
  paths="$(candidate_paths "$commit")" || return 1
  printf '%s\n' "$paths" | python3 "$TIER_TOOL" --explain
}

# 只把分级理由写到 stderr（stdout 丢弃），用于给用户解释为什么不自动发布。
tier_reasons() {
  local commit=$1
  local paths
  paths="$(candidate_paths "$commit")" || return 1
  printf '%s\n' "$paths" | python3 "$TIER_TOOL" --explain >/dev/null
}

# 校验一条队列记录确实来自某个任务的 ship，而不是伪造/陈旧的队列行。
validate_entry() {
  local task=$1 branch=$2 commit=$3
  local meta branch_ref tip
  [[ -n "$task" && -n "$branch" && -n "$commit" ]] || { echo "队列记录缺少字段"; return 1; }
  [[ "$branch" == "agent/$task" ]] || { echo "分支 $branch 与任务 $task 不匹配"; return 1; }
  meta="$TASK_META_DIR/$task.json"
  [[ -r "$meta" ]] || { echo "任务 $task 没有清单 $meta"; return 1; }
  [[ "$(meta_field "$task" branch)" == "$branch" ]] || { echo "任务清单里的分支与队列不一致"; return 1; }
  [[ "$(meta_field "$task" shipped_commit)" == "$commit" ]] \
    || { echo "队列 commit 与任务清单 shipped_commit 不一致（可能被改写）"; return 1; }
  git -C "$MAIN_ROOT" cat-file -e "$commit^{commit}" 2>/dev/null || { echo "commit $commit 不存在"; return 1; }
  branch_ref="refs/heads/$branch"
  if ! tip="$(git -C "$MAIN_ROOT" rev-parse --verify --quiet "$branch_ref" 2>/dev/null)"; then
    echo "分支 $branch 已不存在（发布用 commit 而不是工作区，但分支缺失视为已回收）"
    return 1
  fi
  git -C "$MAIN_ROOT" merge-base --is-ancestor "$commit" "$tip" 2>/dev/null \
    || { echo "commit $commit 不在分支 $branch 上"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
cmd_classify() {
  local task="" offline=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task) task=${2:-}; shift 2 ;;
      --offline) offline=1; shift ;;
      *) fail "classify 未知参数：$1" ;;
    esac
  done
  [[ -n "$task" ]] || fail 'classify 需要 --task <id>'
  fetch_base "$offline"
  local commit
  commit="$(meta_field "$task" shipped_commit)"
  commit="${commit:-$(git -C "$MAIN_ROOT" rev-parse --verify --quiet "refs/heads/agent/$task" 2>/dev/null || true)}"
  [[ -n "$commit" ]] || fail "任务 $task 没有可判定的 commit（先 ship）"
  local tier
  tier="$(tier_of_commit "$commit")" || fail "无法计算 $task 的候选改动（origin/$TARGET_BRANCH 不可用？）"
  info "任务 $task  commit ${commit:0:9}  级别 $tier"
}

cmd_status() {
  info "共享目录：$TASK_META_DIR"
  info "发布队列：$QUEUE_PATH"
  info "流水线游标：$STATE_PATH"
  info ""
  info "== 发布队列 =="
  if [[ -r "$QUEUE_PATH" ]]; then
    local done_set
    done_set="$(state_commits | sort -u)"
    while IFS=$'\t' read -r iso task branch commit; do
      [[ -n "${commit:-}" ]] || continue
      local marker="待处理"
      if printf '%s\n' "$done_set" | grep -qx "$commit"; then
        marker="已处置"
      fi
      printf '  %s  %-24s %-28s %s  [%s]\n' "$iso" "$task" "$branch" "${commit:0:9}" "$marker"
    done < <(queue_lines)
  else
    info "  （空）"
  fi
  info ""
  info "== 已处置（最近 20 条）=="
  if [[ -r "$STATE_PATH" ]]; then
    tail -n 20 "$STATE_PATH" | while IFS=$'\t' read -r iso task commit outcome release; do
      printf '  %s  %-24s %s  %-18s %s\n' "$iso" "$task" "${commit:0:9}" "$outcome" "${release:-}"
    done
  else
    info "  （空）"
  fi
}

# 处置一条 T1 记录。dry_run=1 时只走候选链路，不推送不发布。
handle_t1() {
  local task=$1 branch=$2 commit=$3 dry_run=$4
  local log="$TASK_META_DIR/$task.pipeline.log"
  mkdir -p "$TASK_META_DIR"
  local -a args=(--branch "$branch" --pipeline)
  if [[ "$dry_run" == 1 ]]; then
    args+=(--dry-run)
  else
    trosa_require_release_role \
      || { record_state "$task" "$commit" refused; info "任务 ${task}：当前身份没有发布能力，拒绝发布"; return 1; }
  fi
  info "==> 任务 ${task}（T1）：$([[ "$dry_run" == 1 ]] && echo 门禁演练 || echo 发布) ${commit:0:9}"
  local status=0
  bash "$PUBLISH_ENTRY" "${args[@]}" >"$log" 2>&1 || status=$?
  tail -n 25 "$log" | sed 's/^/    /' || true
  if [[ "$status" == 0 ]]; then
    if [[ "$dry_run" == 1 ]]; then
      record_state "$task" "$commit" dry-run-ok
      info "    结果：dry-run 通过（production 未变更）。日志 $log"
      return 0
    fi
    local release
    release="$(sed -n 's/.*RELEASE_COMMIT_SUCCESS .*release=\([^ ]*\).*/\1/p' "$log" | tail -n 1)"
    merge_task_meta "$task" "status=landed" "landed_commit=$commit" "landed_release=$release" "landed_at=$(utc_now)"
    record_state "$task" "$commit" landed "$release"
    info "    结果：已发布 release=$release"
    return 0
  fi
  record_state "$task" "$commit" failed
  merge_task_meta "$task" "status=active" "verify_result=failed"
  info "    结果：失败（详见 ${log}）。production 未变更。"
  return 1
}

cmd_run() {
  local dry_run=1 task_filter="" limit=0 offline=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      --publish) dry_run=0; shift ;;
      --task) task_filter=${2:-}; shift 2 ;;
      --limit) limit=${2:-0}; shift 2 ;;
      --offline) offline=1; shift ;;
      *) fail "run 未知参数：$1" ;;
    esac
  done

  mkdir -p "$TASK_META_DIR"
  trosa_lock_acquire "$PIPELINE_LOCK" 5 300 \
    || fail '另一个发布流水线实例正在运行，稍后再试'
  trap 'trosa_lock_release "$PIPELINE_LOCK"' EXIT

  fetch_base "$offline" >/dev/null 2>&1 || true
  candidate_base >/dev/null || fail "无法解析 ${BASE_REF}（先 fetch origin ${TARGET_BRANCH}，或用 --offline 且确保本地已有）"

  local done_set
  done_set="$(state_commits | sort -u)"
  local processed=0 rc=0
  while IFS=$'\t' read -r iso task branch commit; do
    [[ -n "${commit:-}" ]] || continue
    if [[ -n "$task_filter" && "$task" != "$task_filter" ]]; then
      continue
    fi
    if printf '%s\n' "$done_set" | grep -qx "$commit"; then
      continue
    fi
    [[ "$limit" == 0 || "$processed" -lt "$limit" ]] || break
    processed=$((processed + 1))

    local problem
    if ! problem="$(validate_entry "$task" "$branch" "$commit" 2>&1)"; then
      record_state "$task" "$commit" refused
      info "跳过 ${task}（$commit 的队列记录不可信）：$problem"
      rc=1
      continue
    fi

    local tier
    if ! tier="$(tier_of_commit "$commit" 2>/dev/null)"; then
      record_state "$task" "$commit" refused
      info "跳过 ${task}：无法计算分级（origin/$TARGET_BRANCH 不可用？）"
      rc=1
      continue
    fi

    case "$tier" in
      T2)
        merge_task_meta "$task" "status=awaiting-approval"
        record_state "$task" "$commit" awaiting-approval
        info "任务 ${task}：T2（触及受保护路径）→ 停在 awaiting-approval，等 approve 后才发布。"
        tier_reasons "$commit" 2>&1 | sed 's/^/    /' || true
        ;;
      T0)
        record_state "$task" "$commit" merge-only
        info "任务 ${task}：T0（纯文档/测试）→ 合入 main 不部署；阶段4 尚未启用该路径，仅记录。"
        ;;
      T1)
        handle_t1 "$task" "$branch" "$commit" "$dry_run" || rc=1
        ;;
      *)
        record_state "$task" "$commit" refused
        info "任务 ${task}：未知级别 ${tier}，拒绝。"
        rc=1
        ;;
    esac
  done < <(queue_lines)

  if [[ "$processed" == 0 ]]; then
    info '没有新的待处置交付。'
  fi
  info "本次处理 $processed 条。"
  return "$rc"
}

main() {
  [[ $# -ge 1 ]] || { usage >&2; exit 1; }
  local command=$1; shift
  case "$command" in
    classify) cmd_classify "$@" ;;
    status) cmd_status "$@" ;;
    run) cmd_run "$@" ;;
    --help|-h|help) usage ;;
    *) usage >&2; fail "未知子命令：$command" ;;
  esac
}

main "$@"
