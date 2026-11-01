#!/bin/bash
# Deploys the crawler on the prod server (P22): ./deploy.sh in ~/equipment-crawler. Pull, rebuild the crawler image,
# apply any change to the database container (Postgres restarts only if its settings changed), run new migrations
# (additive only). It never crawls, tags or spends, and it refuses while a nightly run is going.
# The compose file is chosen by this folder's .env (COMPOSE_FILE=docker-compose.prod.yml on the server).
#
# The body is in main() and called on the last line because `git pull` rewrites this file while it runs, and bash
# reads a script as it goes: without the wrapper it would carry on from the old offset into the new text.
set -euo pipefail

main() {
  cd "$(dirname "$0")"

  if [ -n "$(docker ps -q --filter name=equipment-crawler-crawler-run)" ]; then
    echo "A crawler run is going right now (docker ps shows equipment-crawler-crawler-run). Try again when it ends."
    exit 1
  fi

  echo "Pulling latest code..."
  git pull --ff-only

  echo "Building the crawler image..."
  docker compose build crawler

  echo "Applying database container settings..."
  docker compose up -d --wait pg

  echo "Running migrations (additive only)..."
  ./crawl migrate

  echo "Crawler deploy complete. The next nightly run uses the new code."
}

main "$@"; exit
