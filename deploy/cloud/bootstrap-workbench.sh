#!/usr/bin/env bash
# Prepare a blank Ubuntu ECS instance for one Trade OS service and one
# Cloudflare Tunnel. This does not upload business data or secrets.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TRADE_OS_WORKBENCH_ENV:-$SCRIPT_DIR/workbench.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  printf 'Missing %s. Copy workbench.env.example first.\n' "$ENV_FILE" >&2
  exit 1
fi
source "$ENV_FILE"

: "${TRADE_OS_ECS_REGION:?TRADE_OS_ECS_REGION is required}"
: "${TRADE_OS_ECS_INSTANCE_ID:?TRADE_OS_ECS_INSTANCE_ID is required}"
REMOTE_ROOT="${TRADE_OS_REMOTE_ROOT:-/opt/trade-os}"

workbench upload "$SCRIPT_DIR/../trade-os.service" /tmp/ \
  --instance-id "$TRADE_OS_ECS_INSTANCE_ID" --region "$TRADE_OS_ECS_REGION" --force
workbench upload "$SCRIPT_DIR/../cloudflared.service" /tmp/ \
  --instance-id "$TRADE_OS_ECS_INSTANCE_ID" --region "$TRADE_OS_ECS_REGION" --force

remote_command=$(cat <<EOF
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip curl ca-certificates tar ufw

# The application and Tunnel use loopback plus outbound connections. Keep
# only SSH reachable at the host firewall; 80/443/8080 must not be exposed
# directly from the ECS instance.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

if ! id -u tradeos >/dev/null 2>&1; then
  useradd --system --home-dir /opt/trade-os --create-home --shell /usr/sbin/nologin tradeos
fi
if ! getent group cloudflared >/dev/null 2>&1; then
  groupadd --system cloudflared
fi
if ! id -u cloudflared >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin --gid cloudflared cloudflared
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
  dpkg -i /tmp/cloudflared.deb
  rm -f /tmp/cloudflared.deb
fi

install -d -o root -g root -m 755 "$REMOTE_ROOT" "$REMOTE_ROOT/releases"
install -d -o root -g root -m 755 /etc/trade-os
install -d -o root -g cloudflared -m 750 /etc/cloudflared
install -d -o tradeos -g tradeos -m 700 /var/lib/trade-os

if [ ! -x "$REMOTE_ROOT/venv/bin/python" ]; then
  python3 -m venv "$REMOTE_ROOT/venv"
fi
chown -R root:root "$REMOTE_ROOT/venv"
chmod -R a+rX "$REMOTE_ROOT/venv"

if ! swapon --show --noheadings | grep -q . && [ ! -e /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
fi

install -m 644 /tmp/trade-os.service /etc/systemd/system/trade-os.service
install -m 644 /tmp/cloudflared.service /etc/systemd/system/cloudflared.service
rm -f /tmp/trade-os.service /tmp/cloudflared.service
systemctl daemon-reload
printf '%s\n' 'Trade OS base runtime prepared. Upload production settings and data before enabling services.'
EOF
)

workbench exec \
  --instance-id "$TRADE_OS_ECS_INSTANCE_ID" \
  --region "$TRADE_OS_ECS_REGION" \
  --user-name root \
  --command "$remote_command"
