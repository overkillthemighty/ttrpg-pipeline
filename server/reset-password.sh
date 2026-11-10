#!/bin/sh
# Set a new dashboard password. Run on the Docker host, in this folder:
#     sh reset-password.sh           (lists the accounts, then asks which one)
#     sh reset-password.sh admin
# Runs as PUID:PGID from .env (default 1000:1000) so the database keeps the right owner.
cd "$(dirname "$0")" || exit 1
PUID=$(grep -E '^PUID=' .env 2>/dev/null | tail -n1 | cut -d= -f2)
PGID=$(grep -E '^PGID=' .env 2>/dev/null | tail -n1 | cut -d= -f2)
exec docker compose exec -u "${PUID:-1000}:${PGID:-1000}" ttrpg python -m app.manage reset-password "$@"
