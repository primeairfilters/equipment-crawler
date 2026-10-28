#!/bin/sh
# Adds the read-only eq_readonly login (for DBeaver). Adds only: an existing password file is never overwritten and an
# existing role keeps its password. Connect at localhost:5433 (see docker-compose.yml), database tracforce_equipment.
set -eu
cd "$(dirname "$0")/.." || exit 1

CONF="$HOME/.config/tracforce-crawler"
mkdir -p "$CONF"
chmod 700 "$CONF"
umask 077

if [ ! -e "$CONF/dbeaver.env" ]; then
  printf 'EQ_READONLY_USER=eq_readonly\nEQ_READONLY_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$CONF/dbeaver.env"
  echo "wrote $CONF/dbeaver.env"
fi

docker compose exec -T pg psql -U postgres -d postgres -q \
  -v readonly_pw="$(sed -n 's/^EQ_READONLY_PASSWORD=//p' "$CONF/dbeaver.env")" \
  -f - < setup/pg-readonly.sql
echo "eq_readonly is ready (read-only, SELECT on crawl and kb)."
