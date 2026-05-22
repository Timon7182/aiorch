#!/usr/bin/env bash
# Server-side deploy. Runs in /home/saya/aiorch after `git pull`.
# Reads /home/saya/.aiorch-secrets (export VAR=val) for runtime secrets
# such as GEMINI_API_KEY, then rebuilds and restarts the container.

set -euo pipefail

REPO_ROOT="/home/saya/aiorch"
COMPOSE_FILE="${REPO_ROOT}/.ops/compose.server.yml"
PROJECT_NAME="magesticai-server"
SECRETS_FILE="/home/saya/.aiorch-secrets"

cd "${REPO_ROOT}"

if [[ -r "${SECRETS_FILE}" ]]; then
  set -a
  source "${SECRETS_FILE}"
  set +a
  echo "[deploy] loaded secrets from ${SECRETS_FILE}"
else
  echo "[deploy] WARNING: ${SECRETS_FILE} missing - Hermes will fail without GEMINI_API_KEY"
fi

echo "[deploy] building image"
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" build

echo "[deploy] restarting container"
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --force-recreate

echo "[deploy] polling /api/health"
for i in $(seq 1 40); do
  if curl -sf -o /dev/null http://127.0.0.1:3101/api/health; then
    echo "[deploy] healthy after ${i} polls"
    docker ps --filter "name=magesticai" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    exit 0
  fi
  sleep 3
done

echo "[deploy] FAILED - not healthy in 2 min. Last 80 lines of logs:"
docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" logs --tail=80 app
exit 1
