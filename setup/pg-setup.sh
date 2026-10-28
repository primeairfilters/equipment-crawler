#!/bin/sh
# The one-time root step for the PostgreSQL server (D64): passwords, server, roles, databases, extensions.
# Adds only. Existing password files are never overwritten and existing roles keep their passwords.
set -eu
cd "$(dirname "$0")/.." || exit 1

CONF="$HOME/.config/tracforce-crawler"
mkdir -p "$CONF"
chmod 700 "$CONF"
umask 077

pw() { openssl rand -hex 24; }

if [ ! -e "$CONF/pg-root.env" ]; then
  printf 'POSTGRES_PASSWORD=%s\n' "$(pw)" > "$CONF/pg-root.env"
  echo "wrote $CONF/pg-root.env"
fi
if [ ! -e "$CONF/pg.env" ]; then
  {
    printf 'EQ_DB_HOST=pg\nEQ_DB_PORT=5432\n'
    printf 'EQ_MIGRATE_USER=eq_migrate\nEQ_MIGRATE_PASSWORD=%s\n' "$(pw)"
    printf 'EQ_RUNTIME_USER=eq_runtime\nEQ_RUNTIME_PASSWORD=%s\n' "$(pw)"
    printf 'EQ_BREEZE_USER=eq_breeze\nEQ_BREEZE_PASSWORD=%s\n' "$(pw)"
  } > "$CONF/pg.env"
  echo "wrote $CONF/pg.env"
fi

get() { sed -n "s/^$1=//p" "$CONF/pg.env"; }

docker compose up -d --wait pg
docker compose exec -T pg psql -U postgres -d postgres -q \
  -v migrate_pw="$(get EQ_MIGRATE_PASSWORD)" \
  -v runtime_pw="$(get EQ_RUNTIME_PASSWORD)" \
  -v breeze_pw="$(get EQ_BREEZE_PASSWORD)" \
  -f - < setup/pg-roles.sql
echo "PostgreSQL is ready. Next: ./crawl migrate"
