#!/usr/bin/env bash
# Claude Code PreToolUse 钩子（Edit/Write/NotebookEdit）：写文件之前先过会话租约。
#
# guard/pre-commit 只覆盖“开始”和“提交”，中间写文件全靠 Agent 自觉；这里把租约放到
# 每一次写入前：目标文件所在的 agent/<id> 隔离区若被另一个存活会话占用，写入被拒绝
# （退出码 2，stderr 反馈给模型，内含分叉命令）；空闲时顺手认领/刷新，所以没跑过
# guard 的会话也会被覆盖。目标不在 agent/* 隔离区、缺依赖或脚本异常都一律放行，
# 绝不因护栏自身故障阻断正常开发。
#
# 调用形态由 .claude/settings.json 固定为 `bash "$CLAUDE_PROJECT_DIR"/...`，不依赖
# 可执行位（仓库里的模式可能仍是 100644）。会话标识优先取环境变量；都没有时用事件
# JSON 里的 session_id 兜底，保证钩子自己也能把“当前会话”传给 lease。
set -uo pipefail

payload="$(cat)"
parsed="$(printf '%s' "$payload" | python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get("tool_input") or {}
p = ti.get("file_path") or ti.get("notebook_path") or ""
if p and not os.path.isabs(p):
    p = os.path.join(d.get("cwd") or "", p)
print(d.get("session_id") or "")
print(os.path.dirname(p) if p else "")
' 2>/dev/null)" || exit 0
session_id="${parsed%%$'\n'*}"
target="${parsed#*$'\n'}"
[[ -n "$target" && -d "$target" ]] || exit 0

top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || exit 0
branch="$(git -C "$top" symbolic-ref --short -q HEAD 2>/dev/null)" || exit 0
case "$branch" in agent/*) ;; *) exit 0 ;; esac

script="$top/deploy/cloud/agent-worktree.sh"
[[ -x "$script" ]] || exit 0

# lease 只认 TRADE_OS_AGENT_SESSION / CLAUDE_CODE_SESSION_ID；环境里都没有时用事件
# JSON 的 session_id 兜底，否则钩子会把租约认到“空会话”上而不起作用。
if [[ -z "${TRADE_OS_AGENT_SESSION:-}" && -z "${CLAUDE_CODE_SESSION_ID:-}" && -n "$session_id" ]]; then
  export TRADE_OS_AGENT_SESSION="$session_id"
fi

rc=0
out="$(cd "$top" && "$script" lease check 2>&1 >/dev/null)" || rc=$?
if [[ "$rc" == 42 ]]; then
  printf '%s\n' "$out" >&2
  exit 2
fi
exit 0
