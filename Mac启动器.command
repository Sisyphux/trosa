#!/bin/bash

# CRM Reminder macOS launcher
# Place this file in the project root, alongside app.py and requirements.txt.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

pause_before_exit() {
  printf '\n按回车键关闭窗口...'
  read -r _
}

fail() {
  printf '\n启动失败：%s\n' "$1"
  pause_before_exit
  exit 1
}

printf '\n========================================\n'
printf '       CRM Reminder - Mac 启动器\n'
printf '========================================\n\n'

if [ ! -f "app.py" ] || [ ! -f "requirements.txt" ]; then
  fail "请把“Mac启动器.command”放到 CRM reminder 文件夹内（与 app.py 同一层）。"
fi

if ! command -v python3 >/dev/null 2>&1; then
  fail "未找到 Python 3。请先从 https://www.python.org/downloads/macos/ 安装 Python 3.12。"
fi

PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || true
if [ -z "${PYTHON_VERSION:-}" ]; then
  fail "Python 3 无法正常运行，请重新安装 Python 3.12。"
fi

printf '检测到 Python %s\n' "$PYTHON_VERSION"

# Trade OS production is the single owner of port 8080.  When launchd already
# has it running, this launcher only opens the existing service and exits.
if /usr/bin/curl -fsS --max-time 1 http://127.0.0.1:8080/api/network/ping >/dev/null 2>&1; then
  printf '\n检测到 Trade OS 已在运行，直接打开现有服务。\n'
  /usr/bin/open http://127.0.0.1:8080
  exit 0
fi

VENV_DIR="$SCRIPT_DIR/.venv-mac"
VENV_PYTHON="$VENV_DIR/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
  printf '\n首次运行：正在创建 Mac 运行环境...\n'
  if [ -d "$VENV_DIR" ]; then
    # ZIP 解压可能会把虚拟环境中的符号链接变成普通文本文件；
    # 某些 Python 版本无法用 --clear 修复这类不完整目录。
    # 运行环境不包含业务数据，可以安全删除后重新生成。
    rm -rf "$VENV_DIR" || fail "无法清理损坏的 Mac 运行环境。"
  fi
  python3 -m venv "$VENV_DIR" || fail "无法创建运行环境。"
fi

REQUIREMENTS_STAMP="$VENV_DIR/.requirements-installed"
if [ ! -f "$REQUIREMENTS_STAMP" ] || [ "requirements.txt" -nt "$REQUIREMENTS_STAMP" ]; then
  # 解压后的项目可能保留了依赖目录，却损坏了虚拟环境内的 Python
  # 符号链接。先复用这些纯 Python 依赖，离线时仍可启动。
  FALLBACK_SITE=""
  for candidate in \
    "$SCRIPT_DIR/.venv/lib/python3.12/site-packages" \
    "$SCRIPT_DIR/venv/Lib/site-packages"; do
    if [ -d "$candidate" ]; then
      FALLBACK_SITE="$candidate"
      break
    fi
  done
  TARGET_SITE="$("$VENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"

  if [ -n "$FALLBACK_SITE" ] && [ -n "$TARGET_SITE" ]; then
    printf '\n正在复用项目内已有依赖...\n'
    cp -R "$FALLBACK_SITE"/. "$TARGET_SITE"/ || fail "无法复用项目内已有依赖。"
  else
    printf '\n正在安装项目依赖，请稍候...\n'
  # Python.org's macOS installer may not have initialized its certificate
  # bundle yet. Use the macOS system bundle as a safe first-run fallback.
    if [ -f /etc/ssl/cert.pem ]; then
      SSL_CERT_FILE=/etc/ssl/cert.pem "$VENV_PYTHON" -m pip install --disable-pip-version-check -r requirements.txt
    else
      "$VENV_PYTHON" -m pip install --disable-pip-version-check -r requirements.txt
    fi || fail "依赖安装失败，请检查网络后重试。"
  fi
  "$VENV_PYTHON" -c 'import flask, flask_cors, waitress, apscheduler, requests, openpyxl, email_validator, dns' || \
    fail "项目依赖不完整，请恢复网络后重试。"
  touch "$REQUIREMENTS_STAMP"
fi

printf '\n正在启动 CRM Reminder...\n'
printf '如果浏览器没有自动打开，请访问：http://127.0.0.1:8080\n'
printf '关闭本窗口或按 Control+C 可停止系统。\n\n'

"$VENV_PYTHON" desktop.py
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
  fail "程序异常退出（错误代码：$STATUS）。"
fi

printf '\nCRM Reminder 已停止。\n'
pause_before_exit
