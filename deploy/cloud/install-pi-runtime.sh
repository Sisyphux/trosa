#!/usr/bin/env bash
# Install the pinned, least-privilege runtime used by Trosa's Hamid Pi agent.
# This script installs no CRM credentials and does not restart Trade OS.
set -euo pipefail

NODE_VERSION="${TROSA_PI_NODE_VERSION:-v22.23.2}"
PI_VERSION="${TROSA_PI_VERSION:-0.84.1}"
ROOT="${TROSA_PI_RUNTIME_ROOT:-/opt/trade-os/pi-runtime}"
NODE_DIR="$ROOT/node-$NODE_VERSION"
NODE_ARCHIVE="node-$NODE_VERSION-linux-x64.tar.xz"
NODE_BASE="https://nodejs.org/dist/$NODE_VERSION"
PI_PREFIX="$ROOT/npm"
RELEASE_DIR="${TROSA_PI_RELEASE_DIR:-/opt/trade-os/current}"

case "$NODE_VERSION" in v[0-9]*.[0-9]*.[0-9]*) ;; *) echo "Invalid Node version" >&2; exit 2;; esac
case "$PI_VERSION" in [0-9]*.[0-9]*.[0-9]*) ;; *) echo "Invalid Pi version" >&2; exit 2;; esac
case "$ROOT" in /opt/trade-os/*) ;; *) echo "Runtime root must stay under /opt/trade-os" >&2; exit 2;; esac
case "$RELEASE_DIR" in /opt/trade-os/*) ;; *) echo "Release directory must stay under /opt/trade-os" >&2; exit 2;; esac

install -d -m 0755 -o root -g root "$ROOT"
temporary="$(mktemp -d /tmp/trosa-pi-runtime.XXXXXX)"
cleanup() { rm -rf "$temporary"; }
trap cleanup EXIT

if [[ ! -x "$NODE_DIR/bin/node" ]]; then
  curl --fail --location --silent --show-error --max-time 180 "$NODE_BASE/$NODE_ARCHIVE" -o "$temporary/$NODE_ARCHIVE"
  curl --fail --location --silent --show-error --max-time 60 "$NODE_BASE/SHASUMS256.txt" -o "$temporary/SHASUMS256.txt"
  (cd "$temporary" && grep "  $NODE_ARCHIVE$" SHASUMS256.txt | shasum -a 256 -c -)
  tar -xJf "$temporary/$NODE_ARCHIVE" -C "$temporary"
  install -d -m 0755 -o root -g root "$NODE_DIR"
  cp -a "$temporary/node-$NODE_VERSION-linux-x64/." "$NODE_DIR/"
  chown -R root:root "$NODE_DIR"
fi

export PATH="$NODE_DIR/bin:$PATH"
"$NODE_DIR/bin/npm" install --global --prefix "$PI_PREFIX" --omit=dev --no-audit --no-fund \
  "@earendil-works/pi-coding-agent@$PI_VERSION"
chown -R root:root "$PI_PREFIX"
"$PI_PREFIX/bin/pi" --version | grep -Fx "$PI_VERSION"

if [[ -f "$RELEASE_DIR/pi-agent/package-lock.json" ]]; then
  "$NODE_DIR/bin/npm" ci --omit=dev --ignore-scripts --prefix "$RELEASE_DIR/pi-agent"
  chown -R root:root "$RELEASE_DIR/pi-agent/node_modules"
fi

printf 'TROSA_PI_RUNTIME node=%s pi=%s executable=%s\n' \
  "$("$NODE_DIR/bin/node" --version)" "$PI_VERSION" "$PI_PREFIX/bin/pi"
