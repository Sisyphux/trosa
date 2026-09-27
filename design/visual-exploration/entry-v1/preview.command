#!/bin/bash
# Trosa Entry — 视觉探索预览器
# 双击运行：在本机启动一个临时静态服务并打开四方案对比页。
# 说明：直接双击 index.html 时，部分浏览器会拦截本地 iframe；用本脚本最稳。
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8787}"

if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/index.html"; then
  python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
  sleep 1
fi

open "http://127.0.0.1:$PORT/index.html"
echo "Trosa 视觉探索已打开： http://127.0.0.1:$PORT/index.html"
echo "关闭服务： lsof -ti tcp:$PORT | xargs kill"
