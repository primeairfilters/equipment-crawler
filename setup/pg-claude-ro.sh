#!/bin/sh
# Adds the read-only claude_ro login (Claude's prod checks). Adds only: an existing password file is never overwritten
# and an existing role keeps its password. Reached on the server's 127.0.0.1:5433 (docker-compose.prod.yml) through
# Claude's port-forward-only SSH key.
set -eu
cd "$(dirname "$0")/.." || exit 1

CONF="$HOME/.config/tracforce-crawler"
mkdir -p "$CONF"
chmod 700 "$CONF"
umask 077

if [ ! -e "$CONF/claude-ro.env" ]; then
  printf 'EQ_CLAUDE_RO_USER=claude_ro\nEQ_CLAUDE_RO_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$CONF/claude-ro.env"
  echo "wrote $CONF/claude-ro.env"
fi

docker compose exec -T pg psql -U postgres -d postgres -q \
  -v claude_pw="$(sed -n 's/^EQ_CLAUDE_RO_PASSWORD=//p' "$CONF/claude-ro.env")" \
  -f - < setup/pg-claude-ro.sql
echo "claude_ro is ready (read-only, SELECT on crawl, kb, control and breeze, 10 s per statement)."
