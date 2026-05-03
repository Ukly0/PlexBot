#!/bin/bash
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

echo "[entrypoint] Setting plexbot UID=${PUID} GID=${PGID}"

groupmod -g "$PGID" plexbot 2>/dev/null || true
usermod -u "$PUID" -g "$PGID" plexbot 2>/dev/null || true

mkdir -p /data/tdl
chown -R plexbot:plexbot /data

TDL_HOME="${TDL_HOME:-}"
if [ -n "$TDL_HOME" ]; then
    mkdir -p "$TDL_HOME"
    chown -R plexbot:plexbot "$TDL_HOME"
fi

echo "[entrypoint] Starting PlexBot as plexbot (UID=$(id -u plexbot) GID=$(id -g plexbot))"

exec gosu plexbot "$@"