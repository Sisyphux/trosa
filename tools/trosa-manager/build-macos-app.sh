#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
APP_NAME="服务器工作台"
APP_PATH="$BUILD_DIR/$APP_NAME.app"
CONTENTS="$APP_PATH/Contents"

rm -rf "$APP_PATH"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
swiftc -parse-as-library -O -framework Cocoa \
  "$SCRIPT_DIR/TrosaManager.swift" \
  -o "$CONTENTS/MacOS/server-workbench"
cp "$SCRIPT_DIR/Info.plist" "$CONTENTS/Info.plist"

if [[ "${1:-}" == "--install" ]]; then
  INSTALL_PATH="$HOME/Applications/$APP_NAME.app"
  LEGACY_INSTALL_PATH="$HOME/Applications/trosa Server Manager.app"
  # Replacing an app bundle while an older copy is still running leaves macOS
  # pointing at the old executable. Close that known local manager first.
  osascript -e 'tell application id "com.trosa.server-manager" to quit' >/dev/null 2>&1 || true
  sleep 1
  if [[ -d "$LEGACY_INSTALL_PATH" ]]; then
    LEGACY_TRASH_PATH="$HOME/.Trash/trosa Server Manager.app"
    if [[ -e "$LEGACY_TRASH_PATH" ]]; then
      LEGACY_TRASH_PATH="$HOME/.Trash/trosa Server Manager-$(date +%Y%m%d%H%M%S).app"
    fi
    mv "$LEGACY_INSTALL_PATH" "$LEGACY_TRASH_PATH"
    printf 'moved legacy app to %s\n' "$LEGACY_TRASH_PATH"
  fi
  rm -rf "$INSTALL_PATH"
  mkdir -p "$HOME/Applications"
  ditto "$APP_PATH" "$INSTALL_PATH"
  open "$INSTALL_PATH"
  printf 'installed %s\n' "$INSTALL_PATH"
else
  printf 'built %s\n' "$APP_PATH"
  printf 'run with: open %q\n' "$APP_PATH"
fi
