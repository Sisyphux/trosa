#!/usr/bin/env bash
# 发布前的唯一验证实现：对“即将发布的这棵树”跑完整本地门禁。
#
# 为什么必须只有一份实现：
#   任务区（agent worktree）、release candidate（干净 release worktree）和最终
#   发布的 commit 如果各自有一份测试逻辑，就会出现“在开发机上绿、在发布候选上
#   红”的漂移。2026-09-16 已经真发生一次：tests/test_release_mechanism.py 的
#   三个用例隐式依赖未纳入版本控制的 deploy/cloud/workbench.env，于是它们只在
#   “恰好有本机密钥的那个工作区”里绿过；任何干净检出（新 clone、任务区、
#   release worktree）都红。本脚本保证验证对象永远是当前这棵树，而不是别的树。
#
# 调用方：
#   deploy/cloud/release-commit.sh   干净 release worktree，push 之前
#   deploy/cloud/agent-worktree.sh   test --task（任务区）
#   deploy/cloud/auto-publish.sh     遗留文件清单入口
#
# 用法：
#   release-test.sh [--quick] [--dir DIR]
#     --quick  只做语法检查（不跑 Python 回归与扩展测试）
#     --dir    指定要验证的代码树（默认：本脚本所在的树）
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$SCRIPT_DIR/../.." && pwd)"
# 共享库提供并行门禁运行器；门禁实现本身仍只有这一份 release-test.sh。
# shellcheck source=lib-release-gate.sh
source "$SCRIPT_DIR/lib-release-gate.sh"
# 并行分支进程：库运行器运行期间填充，trap 据此回收。
RELEASE_GATE_PARALLEL_PIDS=()
QUICK=0
TEST_DATA_DIR=""
TEST_ENV_FILE=""
LOG_DIR=""

usage() {
  cat <<'EOF'
Usage:
  release-test.sh [--quick] [--dir DIR]

--quick 只做 Python/JavaScript 语法检查，用于快速反馈；
默认执行完整门禁：快速检查 → 并行（[隔离数据目录 Python 回归（失败重跑一次）] ∥
[真实 PostgreSQL 演练 → 真实 Chromium 页面验收] ∥ [浏览器扩展回归]）。任一分支失败
整道门禁失败。退出码非 0 表示这棵树不可发布。
EOF
}

fail() {
  printf 'release-test: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) QUICK=1; shift ;;
    --dir)
      [[ $# -ge 2 ]] || fail '--dir 需要一个路径'
      [[ -d "$2" ]] || fail "--dir 不是目录：$2"
      TREE="$(cd "$2" && pwd)"
      shift 2
      ;;
    --help|-h) usage; exit 0 ;;
    *) fail "未知参数：$1（--help 查看用法）" ;;
  esac
done

cleanup() {
  local status=$?
  trap - EXIT
  # 并行门禁：先回收仍在运行的子分支，再清理共享临时资源。此处子 shell 已清除
  # 自身 EXIT trap，保证清理只在父进程发生一次。
  local pid
  if [[ ${#RELEASE_GATE_PARALLEL_PIDS[@]} -gt 0 ]]; then
    for pid in "${RELEASE_GATE_PARALLEL_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
    for pid in "${RELEASE_GATE_PARALLEL_PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
  fi
  if [[ -n "$TEST_DATA_DIR" && -d "$TEST_DATA_DIR" ]]; then
    rm -rf -- "$TEST_DATA_DIR"
  fi
  if [[ -n "$TEST_ENV_FILE" && -f "$TEST_ENV_FILE" ]]; then
    rm -f "$TEST_ENV_FILE"
  fi
  if [[ -n "$LOG_DIR" && -d "$LOG_DIR" ]]; then
    rm -rf -- "$LOG_DIR"
  fi
  # 门禁拥有 PostgreSQL rehearsal 服务的生命周期：无论是完整跑完还是中途失败，
  # 都在这里统一停止一次（幂等）。浏览器验收在复用模式下不再停它。
  if [[ -n "$TREE" && -x "${PYTHON_BIN:-}" ]]; then
    "$PYTHON_BIN" "$TREE/tools/postgres_rehearsal.py" stop >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

PYTHON_BIN="$TREE/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || fail "找不到项目 Python：$PYTHON_BIN（先创建 .venv；隔离区可用 symlink 复用主仓 .venv）"
command -v node >/dev/null 2>&1 || fail '找不到 node，无法执行前端语法检查'

cd "$TREE"
printf '\n==> 验证对象 tree=%s\n' "$TREE"
if commit="$(git -C "$TREE" rev-parse HEAD 2>/dev/null)"; then
  printf '     commit=%s\n' "$commit"
  if [[ -n "$(git -C "$TREE" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
    printf '     注意：该树存在未提交的已跟踪改动；验证结果对应的是工作区内容，不是 commit\n' >&2
  fi
else
  printf '     注意：该目录不是 Git 工作树；验证结果无法锚定到任何 commit\n' >&2
fi

printf '\n==> Python 语法检查\n'
"$PYTHON_BIN" -m py_compile app.py db.py scheduler.py serve.py serve_rehearsal.py
printf '完成：Python 语法检查\n'

printf '\n==> 迁移目录完整性（编号唯一、可被运行时应用）\n'
"$PYTHON_BIN" "$TREE/tools/check_migrations.py" --dir "$TREE"
printf '完成：迁移目录完整性\n'

printf '\n==> 前端 JavaScript 语法检查\n'
node --check app/static/app.js
printf '完成：前端 JavaScript 语法检查\n'

if [[ "$QUICK" == 1 ]]; then
  printf '\nrelease-test: OK tree=%s quick=1\n' "$TREE"
  exit 0
fi

command -v npm >/dev/null 2>&1 || fail '找不到 npm，无法执行浏览器扩展回归'
[[ -d "$TREE/browser-extension/node_modules" ]] \
  || fail "找不到 $TREE/browser-extension/node_modules（先在 browser-extension 执行 npm install；隔离区可用 symlink 复用主仓）"

# Some source-level release tests invoke trosa-release for argument/routing
# checks. They must not depend on a developer's real workbench.env (which is
# intentionally absent from clean clones and release worktrees), and they must
# never inherit credentials or production routing during a local regression.
TEST_ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/trosa-release-test-env.XXXXXX")"
{
  printf 'TRADE_OS_ECS_REGION=test-region\n'
  printf 'TRADE_OS_ECS_INSTANCE_ID=i-test-instance\n'
} >"$TEST_ENV_FILE"

# 并行分支通过共享日志目录汇总输出；父进程在 trap 中统一回收。
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trosa-release-gate.XXXXXX")"
# 一整条门禁只选一个 rehearsal 端口，Python 集成测试与浏览器验收共用同一个
# loopback 服务，避免第二次调用时因端口不同而停-起一次 PostgreSQL。
REHEARSAL_GATE_PORT="${TROSA_RELEASE_REHEARSAL_PORT:-}"
if [[ -z "$REHEARSAL_GATE_PORT" ]]; then
  REHEARSAL_GATE_PORT="$("$PYTHON_BIN" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export TROSA_REHEARSAL_PORT="$REHEARSAL_GATE_PORT"

# 隔离数据目录由父进程创建并回收：并行子 shell 里的赋值不会传回父进程。
TEST_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trosa-release-test.XXXXXX")"

run_python_regression() {
  # 复用父进程创建的目录；重跑时清空重建，保证每次都是隔离数据目录。
  rm -rf -- "$TEST_DATA_DIR"
  mkdir -p -- "$TEST_DATA_DIR"
  # Never inherit a developer's formal PostgreSQL settings or rehearsal DSN.
  # The default gate is an isolated SQLite regression; PostgreSQL acceptance is
  # an explicit, separate rehearsal command.
  #
  # Also drop the caller's agent role (TRADE_OS_AGENT_ROLE). The gate is the
  # neutral verification environment: when a dev/review session runs it, the
  # source-level release tests must not inherit that role and have the release
  # boundaries reject their own stubbed publish calls. The role guard itself is
  # still covered explicitly by tests/test_release_boundaries.py, which sets the
  # role per subprocess instead of relying on ambient state.
  env \
    -u TRADE_OS_AGENT_ROLE \
    -u TRADE_OS_DATABASE_URL \
    -u PGPASSFILE \
    -u TROSA_REHEARSAL \
    -u TROSA_REHEARSAL_DATABASE_URL \
    CRM_ENV=development \
    TRADE_OS_DEV_SQLITE=1 \
    TRADE_OS_DATA_BACKEND=sqlite \
    TRADE_OS_WORKBENCH_ENV="$TEST_ENV_FILE" \
    CRM_DB_PATH="$TEST_DATA_DIR" \
    "$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py' -v
}

# 分支 A：隔离 SQLite 的 Python 回归（失败重跑一次，两次都失败才红）。
run_python_regression_branch() {
  trap - EXIT   # 清理只由父进程负责
  printf '\n==> Python 回归测试（隔离数据目录）\n'
  if run_python_regression; then
    printf '完成：Python 回归测试\n'
    return 0
  else
    first_status=$?
    printf '失败：Python 回归测试（退出码 %s），重跑一次以排除瞬时竞态\n' "$first_status" >&2
    sleep 1
    if run_python_regression; then
      printf '完成：Python 回归测试（第 2 次运行通过）\n'
      return 0
    else
      second_status=$?
      fail "Python 回归测试两次均失败（第一次 %s，第二次 %s）" "$first_status" "$second_status"
    fi
  fi
}

# 分支 B：真实 PostgreSQL rehearsal → 真实 Chromium 页面验收。与分支 A（SQLite）
# 和分支 C（扩展回归）互不依赖；同一门禁里 PostgreSQL 服务只起停一次。
run_rehearsal_browser_branch() {
  trap - EXIT
  printf '\n==> PostgreSQL rehearsal（真实 loopback PostgreSQL）\n'
  TROSA_REHEARSAL_PORT="$REHEARSAL_GATE_PORT" CRM_ENV=rehearsal \
    "$PYTHON_BIN" "$TREE/tools/postgres_rehearsal.py" test \
    || fail 'PostgreSQL rehearsal failed; this gate never converts it to SKIP'
  printf '完成：PostgreSQL rehearsal\n'

  printf '\n==> 真实 Chromium 页面验收（Customer → 沟通 → Today → Inbox → Search）\n'
  [[ -x "$TREE/tools/browser_acceptance.sh" ]] \
    || fail "找不到真实浏览器验收入口：$TREE/tools/browser_acceptance.sh"
  # 复用上面已在同一端口启动并迁移好的 rehearsal 服务/连接；浏览器验收只重载
  # 确定性 fixture（PostgreSQL 集成测试改动了数据），不再重启服务，也不在退出时
  # 停掉门禁拥有的服务。服务生命周期由本门禁统一回收（见 cleanup）。
  eval "$("$PYTHON_BIN" "$TREE/tools/postgres_rehearsal.py" env)"
  export TROSA_REHEARSAL_PORT="$REHEARSAL_GATE_PORT"
  TROSA_BROWSER_REHEARSAL_PORT="$REHEARSAL_GATE_PORT" \
    TROSA_BROWSER_ACCEPTANCE_REUSE_REHEARSAL=1 \
    "$TREE/tools/browser_acceptance.sh" \
    || fail '真实 Chromium 页面验收失败；不会以 DOM/语法测试代替'
  printf '完成：真实 Chromium 页面验收\n'
}

# 分支 C：浏览器扩展回归。与 A/B 完全独立，因此并行执行。
run_extension_branch() {
  trap - EXIT
  printf '\n==> 浏览器扩展回归测试\n'
  (cd "$TREE/browser-extension" && npm test) || fail '浏览器扩展回归测试失败'
  printf '完成：浏览器扩展回归测试\n'
}

printf '\n==> 并行门禁：Python 回归 ∥ PostgreSQL/浏览器验收 ∥ 扩展回归（输出按分支分组）\n'
# 运行器同时等待所有分支；任一失败仍等待其余分支结束，不留下后台进程，退出码向上传播。
release_gate_run_parallel "$LOG_DIR" \
  run_python_regression_branch run_rehearsal_browser_branch run_extension_branch \
  || fail "门禁失败：Python 回归 / PostgreSQL 浏览器验收 / 扩展回归有分支未通过（见上方分支输出）"

printf '\nrelease-test: OK tree=%s\n' "$TREE"
