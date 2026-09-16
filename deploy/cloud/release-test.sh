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
QUICK=0
TEST_DATA_DIR=""

usage() {
  cat <<'EOF'
Usage:
  release-test.sh [--quick] [--dir DIR]

--quick 只做 Python/JavaScript 语法检查，用于快速反馈；
默认执行完整门禁：语法检查 → 隔离数据目录 Python 回归（失败重跑一次）→
浏览器扩展回归。退出码非 0 表示这棵树不可发布。
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
  if [[ -n "$TEST_DATA_DIR" && -d "$TEST_DATA_DIR" ]]; then
    rm -rf -- "$TEST_DATA_DIR"
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

run_python_regression() {
  if [[ -n "$TEST_DATA_DIR" && -d "$TEST_DATA_DIR" ]]; then
    rm -rf -- "$TEST_DATA_DIR"
  fi
  TEST_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trosa-release-test.XXXXXX")"
  # Never inherit a developer's formal PostgreSQL settings or rehearsal DSN.
  # The default gate is an isolated SQLite regression; PostgreSQL acceptance is
  # an explicit, separate rehearsal command.
  env \
    -u TRADE_OS_DATABASE_URL \
    -u PGPASSFILE \
    -u TROSA_REHEARSAL \
    -u TROSA_REHEARSAL_DATABASE_URL \
    CRM_ENV=development \
    TRADE_OS_DEV_SQLITE=1 \
    TRADE_OS_DATA_BACKEND=sqlite \
    CRM_DB_PATH="$TEST_DATA_DIR" \
    "$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py' -v
}

printf '\n==> Python 回归测试（隔离数据目录）\n'
if run_python_regression; then
  printf '完成：Python 回归测试\n'
else
  first_status=$?
  printf '失败：Python 回归测试（退出码 %s），重跑一次以排除瞬时竞态\n' "$first_status" >&2
  sleep 1
  if run_python_regression; then
    printf '完成：Python 回归测试（第 2 次运行通过）\n'
  else
    second_status=$?
    fail "Python 回归测试两次均失败（第一次 %s，第二次 %s）" "$first_status" "$second_status"
  fi
fi

printf '\n==> 浏览器扩展回归测试\n'
(cd "$TREE/browser-extension" && npm test) || fail '浏览器扩展回归测试失败'
printf '完成：浏览器扩展回归测试\n'

printf '\nrelease-test: OK tree=%s\n' "$TREE"
