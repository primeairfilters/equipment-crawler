#!/bin/sh
# Adds the eq_admin login for Tracforce's /admin page (P19). Adds only: an existing password file is never overwritten
# and an existing role keeps its password. Its rights on the control schema come from ./crawl migrate.
set -eu
cd "$(dirname "$0")/.." || exit 1

CONF="$HOME/.config/tracforce-crawler"
mkdir -p "$CONF"
chmod 700 "$CONF"
umask 077

if [ ! -e "$CONF/admin.env" ]; then
  printf 'EQ_ADMIN_USER=eq_admin\nEQ_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$CONF/admin.env"
  echo "wrote $CONF/admin.env"
fi

docker compose exec -T pg psql -U postgres -d postgres -q \
  -v admin_pw="$(sed -n 's/^EQ_ADMIN_PASSWORD=//p' "$CONF/admin.env")" \
  -f - < setup/pg-admin.sql
echo "eq_admin is ready (connect only until the control schema migration)."
