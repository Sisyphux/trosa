#!/bin/zsh
# Double-click this file in Finder to run the same production health check.
set -u

PROJECT_DIR="${0:A:h}"
SERVICE_SCRIPT="/Users/luoxin/Library/Application Support/TradeOS/check-and-repair.sh"
SOURCE_SCRIPT="$PROJECT_DIR/deploy/macos/check-and-repair.sh"

if [[ -x "$SERVICE_SCRIPT" ]]; then
  "$SERVICE_SCRIPT"
else
  "$SOURCE_SCRIPT"
fi
STATUS=$?

if (( STATUS == 0 )); then
  print "检查完成。"
else
  print -u2 "检查或修复未完成（退出码：$STATUS）。"
fi
print "按任意键关闭窗口。"
read -rk 1
exit "$STATUS"
