#!/usr/bin/env bash
# 发布门禁对象身份：让 release 角色在“同一棵树 + 同一门禁实现 + 同一外部输入 +
# 同一基线”时复用已验收结论，而不是把同一棵树重算一遍。任何一项不匹配都会 miss，
# 调用方必须回到全量门禁（fail closed）。
#
# 为什么按对象身份而不是按任务目录
# --------------------------------
# 开发结论只在“被验收的那棵树”上成立。任务目录可以继续改文件，候选树则是
# cherry-pick 后不可变 commit 对应的 git tree。用 tree hash 锚定对象，
# release 侧不需要、也不读取任何任务工作区路径。
#
# 三项锚定（外加基线）
# --------------------
#   tree       候选项的 git tree hash（逐字节内容身份，HEAD^{tree}）
#   gate_impl  实际执行门禁的实现：运行门禁那份 deploy/cloud（release-test.sh 等）
#              + 候选树 tools/（browser_acceptance、postgres_rehearsal 等）的
#              路径 + blob 哈希。只改测试代码（tests/）不会命中这一段，但会被
#              tree hash 捕获。
#   external   候选树 migrations/ 目录（文件名 + 内容）与门禁固定的测试环境占位。
#   base       origin/main 的 commit sha。基线一旦前进，即使树巧合相同也回到全量。
#
# 账本只登记 result=ok 且 key 完整的记录：写入方必须先确认工作树干净，
# 否则“被验收的内容”不等于 HEAD 的 tree。查找 miss 一律返回非 0。

RELEASE_GATE_LEDGER_NAME=".verified-trees"

# sha256：优先 shasum（macOS），否则 sha256sum（Linux）。
release_gate_sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    sha256sum | awk '{print $1}'
  fi
}

# 门禁实现哈希：deploy/cloud 来自实际运行门禁的树 gate_tree，tools 来自候选树。
release_gate_impl_hash() {
  local candidate_tree=$1 gate_tree=$2
  {
    git -C "$gate_tree" ls-files -s -- deploy/cloud 2>/dev/null \
      | awk '{printf "dc %s %s %s\n", $1, $2, $4}'
    git -C "$candidate_tree" ls-files -s -- tools 2>/dev/null \
      | awk '{printf "tl %s %s %s\n", $1, $2, $4}'
  } | LC_ALL=C sort | release_gate_sha256
}

# 外部输入哈希：候选树 migrations/ 目录内容 + 门禁固定测试环境占位。
# release-test.sh 每次都用同一份固定的 dummy 路由文件（test-region /
# i-test-instance）隔离测试；这里把它作为常量锚定，任何改动都会改变外部输入。
release_gate_external_hash() {
  local tree=$1
  local mig="$tree/migrations" f
  {
    if [[ -d "$mig" ]]; then
      while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        printf 'm %s\n' "${f#"$tree"/}"
        release_gate_sha256 <"$f"
      done < <(find "$mig" -type f 2>/dev/null | LC_ALL=C sort)
    fi
    printf 'env trosa-release-test-env:test-region:i-test-instance\n'
  } | release_gate_sha256
}

# 计算对象身份，输出：<key>\t<tree>\t<gate_impl>\t<external>\t<base>
# 任一组成部分解析失败即返回非 0（调用方必须回到全量门禁）。
# 用法：release_gate_identity <candidate_tree> <gate_tree> <base_sha>
release_gate_identity() {
  local candidate_tree=$1 gate_tree=$2 base=$3
  local tree gate_impl external key
  [[ -n "$candidate_tree" && -n "$gate_tree" ]] || return 1
  tree="$(git -C "$candidate_tree" rev-parse --verify --quiet 'HEAD^{tree}' 2>/dev/null)" || return 1
  [[ -n "$tree" ]] || return 1
  gate_impl="$(release_gate_impl_hash "$candidate_tree" "$gate_tree")" || return 1
  [[ -n "$gate_impl" ]] || return 1
  external="$(release_gate_external_hash "$candidate_tree")" || return 1
  [[ -n "$external" ]] || return 1
  key="$(printf 'tree=%s\ngate_impl=%s\nexternal=%s\nbase=%s\n' \
    "$tree" "$gate_impl" "$external" "$base" | release_gate_sha256)" || return 1
  [[ -n "$key" ]] || return 1
  printf '%s\t%s\t%s\t%s\t%s\n' "$key" "$tree" "$gate_impl" "$external" "$base"
}

release_gate_ledger_path() {
  printf '%s/trosa-tasks/%s' "$1" "$RELEASE_GATE_LEDGER_NAME"
}

# 登记一条已验收记录。调用方必须保证 result=ok 且工作树干净。
# 这里只做单行 append（本地文件 O_APPEND 足够原子），重复记录无害。
release_gate_register() {
  local git_common=$1 identity=$2 task=$3
  local key tree gate_impl external base at ledger line
  IFS=$'\t' read -r key tree gate_impl external base <<<"$identity"
  [[ -n "$key" && -n "$tree" && -n "$gate_impl" && -n "$external" && -n "$base" ]] || return 1
  ledger="$(release_gate_ledger_path "$git_common")"
  mkdir -p -- "$(dirname "$ledger")" || return 1
  at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  line="key=$key task=$task tree=$tree gate_impl=$gate_impl external=$external base=$base at=$at"
  printf '%s\n' "$line" >>"$ledger"
}

# 查找已验收记录：命中输出匹配行，miss 返回非 0。绝不读取任何任务目录。
release_gate_lookup() {
  local git_common=$1 identity=$2
  local key ledger
  key="${identity%%$'\t'*}"
  [[ -n "$key" ]] || return 1
  ledger="$(release_gate_ledger_path "$git_common")"
  [[ -r "$ledger" ]] || return 1
  grep -m1 -E "^key=${key}([[:space:]]|\$)" "$ledger"
}

# 并行运行多个门禁分支函数：任一失败仍等待其余分支结束后返回非 0，输出先分别
# 落盘再按分支顺序打印（不交错）。运行的子进程 pid 暴露在
# RELEASE_GATE_PARALLEL_PIDS，供调用方的 EXIT trap 在被打断时回收。
# 用法：release_gate_run_parallel <log_dir> <branch_fn>...
release_gate_run_parallel() {
  local log_dir=$1
  shift
  local branches=("$@")
  local pids=() names=() pid rc=0 rc_one=0 i=0 fn
  [[ ${#branches[@]} -gt 0 ]] || return 0
  RELEASE_GATE_PARALLEL_PIDS=()
  for fn in "${branches[@]}"; do
    "$fn" >"$log_dir/gate-$i.log" 2>&1 &
    pid=$!
    RELEASE_GATE_PARALLEL_PIDS+=("$pid")
    pids+=("$pid")
    names+=("$fn")
    i=$((i + 1))
  done
  set +e
  for pid in "${pids[@]}"; do
    wait "$pid"; rc_one=$?
    [[ "$rc_one" == 0 ]] || rc=1
  done
  set -e
  RELEASE_GATE_PARALLEL_PIDS=()
  i=0
  for fn in "${names[@]}"; do
    printf '\n----- 分支 %s 输出 -----\n' "$fn"
    cat "$log_dir/gate-$i.log"
    i=$((i + 1))
  done
  [[ "$rc" == 0 ]]
}
