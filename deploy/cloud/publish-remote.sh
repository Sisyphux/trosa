#!/usr/bin/env bash
# Download and atomically publish one public GitHub commit on ECS.
set -euo pipefail

if [ "$#" -ne 5 ]; then
  printf 'Usage: %s REMOTE_ROOT SERVICE_NAME RELEASE_ID COMMIT_SHA GITHUB_REMOTE\n' "$0" >&2
  exit 2
fi

REMOTE_ROOT=$1
SERVICE_NAME=$2
RELEASE_ID=$3
COMMIT_SHA=$4
GITHUB_REMOTE=$5

case "$REMOTE_ROOT" in
  ''|*[!A-Za-z0-9._/-]*) printf 'Invalid remote root: %s\n' "$REMOTE_ROOT" >&2; exit 2 ;;
esac
case "$SERVICE_NAME" in
  ''|*[!A-Za-z0-9_.@-]*) printf 'Invalid service name: %s\n' "$SERVICE_NAME" >&2; exit 2 ;;
esac
case "$RELEASE_ID" in
  ''|*[!A-Za-z0-9._-]*) printf 'Invalid release id: %s\n' "$RELEASE_ID" >&2; exit 2 ;;
esac
case "$COMMIT_SHA" in
  ''|*[!0-9a-fA-F]*) printf 'Invalid commit sha.\n' >&2; exit 2 ;;
esac
if [ "${#COMMIT_SHA}" -ne 40 ]; then
  printf 'Invalid commit sha length.\n' >&2
  exit 2
fi
case "$GITHUB_REMOTE" in
  https://github.com/[A-Za-z0-9_.-]*/[A-Za-z0-9_.-]*) ;;
  *) printf 'Invalid GitHub remote: %s\n' "$GITHUB_REMOTE" >&2; exit 2 ;;
esac

ARCHIVE_NAME="trosa-$COMMIT_SHA.tar.gz"
ARCHIVE_PATH="/tmp/$ARCHIVE_NAME"
RELEASE_DIR="$REMOTE_ROOT/releases/$RELEASE_ID"
PREVIOUS=$(readlink -f "$REMOTE_ROOT/current" 2>/dev/null || true)

curl --fail --location --silent --show-error --max-time 180 \
  "$GITHUB_REMOTE/archive/$COMMIT_SHA.tar.gz" \
  -o "$ARCHIVE_PATH"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
tar -xzf "$ARCHIVE_PATH" -C "$RELEASE_DIR" --strip-components=1
"$REMOTE_ROOT/venv/bin/pip" install --disable-pip-version-check -r "$RELEASE_DIR/requirements.txt"
"$REMOTE_ROOT/venv/bin/python" -m py_compile \
  "$RELEASE_DIR/app.py" "$RELEASE_DIR/db.py" "$RELEASE_DIR/scheduler.py" "$RELEASE_DIR/serve.py"
chown -R root:root "$RELEASE_DIR"
ln -sfn "$RELEASE_DIR" "$REMOTE_ROOT/current.next"
mv -Tf "$REMOTE_ROOT/current.next" "$REMOTE_ROOT/current"
systemctl daemon-reload
if ! systemctl restart "$SERVICE_NAME"; then
  if [ -n "$PREVIOUS" ]; then
    ln -sfn "$PREVIOUS" "$REMOTE_ROOT/current.next"
    mv -Tf "$REMOTE_ROOT/current.next" "$REMOTE_ROOT/current"
    systemctl restart "$SERVICE_NAME" || true
  fi
  exit 1
fi

healthy=0
for attempt in $(seq 1 15); do
  if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8080/api/network/ping >/dev/null; then
    healthy=1
    break
  fi
  sleep 1
done
if [ "$healthy" != 1 ]; then
  if [ -n "$PREVIOUS" ]; then
    ln -sfn "$PREVIOUS" "$REMOTE_ROOT/current.next"
    mv -Tf "$REMOTE_ROOT/current.next" "$REMOTE_ROOT/current"
    systemctl restart "$SERVICE_NAME" || true
  fi
  journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
  exit 1
fi

rm -f "$ARCHIVE_PATH"
find "$REMOTE_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -print | sort -r | tail -n +6 | xargs -r rm -rf
printf 'published %s\n' "$RELEASE_ID"
