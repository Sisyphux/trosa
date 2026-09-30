#!/usr/bin/env bash
# Claude Code PreToolUse 钩子（Edit/Write/NotebookEdit）：写文件之前先过会话租约。
#
# guard/pre-commit 只覆盖“开始”和“提交”，中间写文件全靠 Agent 自觉；这里把租约放到
# 每一次写入前：目标文件所在的 agent/<id> 隔离区若被另一个存活会话占用，写入被拒绝
# （退出码 2，stderr 反馈给模型，内含分叉命令）；空闲时顺手认领/刷新，所以没跑过
# guard 的会话也会被覆盖。目标不在 agent/* 隔离区、缺依赖或脚本异常都一律放行，
# 绝不因护栏自身故障阻断正常开发。
set -uo pipefail

payload="$(cat)"
target="$(printf '%s' "$payload" | python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get("tool_input") or {}
p = ti.get("file_path") or ti.get("notebook_path") or ""
if p and not os.path.isabs(p):
    p = os.path.join(d.get("cwd") or "", p)
print(os.path.dirname(p) if p else "")
' 2>/dev/null)"
[[ -n "$target" && -d "$target" ]] || exit 0

top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || exit 0
branch="$(git -C "$top" symbolic-ref --short -q HEAD 2>/dev/null)" || exit 0
case "$branch" in agent/*) ;; *) exit 0 ;; esac

script="$top/deploy/cloud/agent-worktree.sh"
[[ -x "$script" ]] || exit 0
rc=0
out="$(cd "$top" && "$script" lease check 2>&1 >/dev/null)" || rc=$?
if [[ "$rc" == 42 ]]; then
  printf '%s\n' "$out" >&2
  exit 2
fi
exit 0
