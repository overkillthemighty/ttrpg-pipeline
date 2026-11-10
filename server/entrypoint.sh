#!/bin/sh
# Make sure the data volume is writable by the app user, then drop root.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
export HOME=/data/models/home
mkdir -p /data/jobs /data/models "$HOME"
if [ "$(id -u)" = "0" ]; then
  # Fix anything not owned by the app user (e.g. left by an earlier run as root or another UID)
  find /data -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
