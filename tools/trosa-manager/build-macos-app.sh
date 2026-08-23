#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
APP_PATH="$BUILD_DIR/trosa Server Manager.app"
CONTENTS="$APP_PATH/Contents"

rm -rf "$APP_PATH"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
swiftc -parse-as-library -O -framework Cocoa \
  "$SCRIPT_DIR/TrosaManager.swift" \
  -o "$CONTENTS/MacOS/trosa-server-manager"
cp "$SCRIPT_DIR/Info.plist" "$CONTENTS/Info.plist"

if [[ "${1:-}" == "--install" ]]; then
  INSTALL_PATH="$HOME/Applications/trosa Server Manager.app"
  # Replacing an app bundle while an older copy is still running leaves macOS
  # pointing at the old executable. Close that known local manager first.
  osascript -e 'tell application id "com.trosa.server-manager" to quit' >/dev/null 2>&1 || true
  sleep 1
  rm -rf "$INSTALL_PATH"
  mkdir -p "$HOME/Applications"
  ditto "$APP_PATH" "$INSTALL_PATH"
  open "$INSTALL_PATH"
  printf 'installed %s\n' "$INSTALL_PATH"
else
  printf 'built %s\n' "$APP_PATH"
  printf 'run with: open %q\n' "$APP_PATH"
fi
