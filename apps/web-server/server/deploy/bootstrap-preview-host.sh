#!/usr/bin/env bash
#
# bootstrap-preview-host.sh — one-command setup of the preview host (192.168.88.55).
#
# Stands up everything the preview/static-lane feature needs:
#   1. a dedicated Postgres 16 container (isolated from other DBs on the host)
#   2. jq + postgresql-client
#   3. the preview-runner + /etc/magestic-preview/preview.env
#   4. the golden + static lane DBs (via setup-golden-dbs.sh)
#   5. (optional) the two standing static lanes (main + test) -> 2 stable URLs
#
# Idempotent: safe to re-run. Run as a user that can sudo + use docker.
#
# Usage:
#   ./bootstrap-preview-host.sh --pg-pass <password> \
#       [--pg-port 5436] [--public-host 192.168.88.55] \
#       [--dump /path/cts.dump | --src-ssh cargo-preprod --src-db <db> \
#                                 --src-container cts-db-postgres-1 --src-user admin] \
#       [--sanitize ./sanitize.sql] \
#       [--cts-root /home/saya/projects/cts]   # enables initial static lanes

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

PG_PASS=""; PG_PORT="5436"; PUBLIC_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
DUMP=""; SANITIZE=""; CTS_ROOT=""
SRC_SSH=""; SRC_DB="cts"; SRC_CONTAINER="cts-db-postgres-1"; SRC_USER="admin"
PG_CONTAINER="cts-preview-pg"
PG_VOLUME="cts-preview-pg-data"
STATE_DIR="${MAGESTIC_PREVIEW_STATE:-/home/$USER/.magestic-preview}"

log(){ printf '[bootstrap] %s\n' "$*" >&2; }
die(){ log "ERROR: $*"; exit 1; }

while [[ $# -gt 0 ]]; do case "$1" in
  --pg-pass) PG_PASS="${2:?}"; shift 2;;
  --pg-port) PG_PORT="${2:?}"; shift 2;;
  --public-host) PUBLIC_HOST="${2:?}"; shift 2;;
  --dump) DUMP="${2:?}"; shift 2;;
  --sanitize) SANITIZE="${2:?}"; shift 2;;
  --cts-root) CTS_ROOT="${2:?}"; shift 2;;
  --src-ssh) SRC_SSH="${2:?}"; shift 2;;
  --src-db) SRC_DB="${2:?}"; shift 2;;
  --src-container) SRC_CONTAINER="${2:?}"; shift 2;;
  --src-user) SRC_USER="${2:?}"; shift 2;;
  *) die "unknown arg: $1";;
esac; done

[[ -n "$PG_PASS" ]] || die "--pg-pass is required (password for the dedicated preview Postgres superuser 'admin')"
command -v docker >/dev/null || die "docker not found"

# ---------------------------------------------------------------------------
# 1. dedicated Postgres 16 for goldens + preview clones + static lanes
# ---------------------------------------------------------------------------
if ! docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  log "creating dedicated Postgres container $PG_CONTAINER on port $PG_PORT"
  docker volume create "$PG_VOLUME" >/dev/null
  docker run -d --name "$PG_CONTAINER" --restart unless-stopped \
    -e POSTGRES_USER=admin -e POSTGRES_PASSWORD="$PG_PASS" \
    -p "${PG_PORT}:5432" -v "${PG_VOLUME}:/var/lib/postgresql/data" \
    postgres:16 >/dev/null
  log "waiting for Postgres to accept connections..."
  for _ in $(seq 1 30); do
    docker exec "$PG_CONTAINER" pg_isready -U admin >/dev/null 2>&1 && break; sleep 2
  done
else
  log "Postgres container $PG_CONTAINER already exists — reusing"
fi

# docker bridge gateway so preview containers can reach the host-published PG
BRIDGE_GW="$(docker network inspect bridge -f '{{ (index .IPAM.Config 0).Gateway }}' 2>/dev/null || echo 172.17.0.1)"

# ---------------------------------------------------------------------------
# 2. tools
# ---------------------------------------------------------------------------
if ! command -v jq >/dev/null || ! command -v psql >/dev/null; then
  log "installing jq + postgresql-client"
  sudo apt-get update -y && sudo apt-get install -y jq postgresql-client
fi

# ---------------------------------------------------------------------------
# 3. runner + preview.env
# ---------------------------------------------------------------------------
log "installing preview-runner -> /usr/local/bin/preview-runner"
sudo install -m0755 "$HERE/preview-runner.sh" /usr/local/bin/preview-runner

log "writing /etc/magestic-preview/preview.env"
sudo install -d -m0755 /etc/magestic-preview
sudo tee /etc/magestic-preview/preview.env >/dev/null <<EOF
PGHOST=127.0.0.1
PGPORT=${PG_PORT}
PGUSER=admin
PGPASSWORD=${PG_PASS}
PGADMIN_DB=postgres
PREVIEW_DB_HOST_FOR_CONTAINERS=${BRIDGE_GW}
PREVIEW_GOLDEN_DB_A=cts_golden_a
PREVIEW_GOLDEN_DB_B=cts_golden_b
PREVIEW_STATIC_DB_A=cts_static_a
PREVIEW_STATIC_DB_B=cts_static_b
PREVIEW_PUBLIC_HOST=${PUBLIC_HOST}
PREVIEW_PORT_MIN=12000
PREVIEW_PORT_MAX=12999
PREVIEW_STATIC_PORT_A=13001
PREVIEW_STATIC_PORT_B=13002
PREVIEW_DOCKER_NETWORK=bridge
PREVIEW_NGINX_ENABLED=0
PREVIEW_MAX_CONCURRENT=2
MAGESTIC_PREVIEW_STATE=${STATE_DIR}
EOF
sudo chmod 600 /etc/magestic-preview/preview.env
mkdir -p "$STATE_DIR"

# ---------------------------------------------------------------------------
# 4. golden + static DBs
# ---------------------------------------------------------------------------
log "building golden + static DBs"
setup_args=()
[[ -n "$DUMP" ]] && setup_args+=(--dump "$DUMP")
[[ -n "$SANITIZE" ]] && setup_args+=(--sanitize "$SANITIZE")
if [[ -z "$DUMP" && -n "$SRC_SSH" ]]; then
  export SRC_SSH SRC_DB SRC_PG_CONTAINER="$SRC_CONTAINER" SRC_PG_USER="$SRC_USER"
fi
PREVIEW_ENV=/etc/magestic-preview/preview.env bash "$HERE/setup-golden-dbs.sh" "${setup_args[@]}"

# ---------------------------------------------------------------------------
# 5. optional: stand up the two static lanes (gives the 2 standing URLs now)
# ---------------------------------------------------------------------------
if [[ -n "$CTS_ROOT" ]]; then
  [[ -d "$CTS_ROOT" ]] || die "--cts-root not found: $CTS_ROOT"
  CFG="$CTS_ROOT/deploy.config.json"
  [[ -f "$CFG" ]] || CFG="$HERE/deploy.config.example.json"
  log "initial static deploy: lane A (main) and lane B (test) from $CTS_ROOT (config $CFG)"
  # Deploy a throwaway preview from the current source then immediately promote
  # it to the static lane (this is exactly what the UI's Promote does).
  preview-runner deploy   --task static-init-a --lane A --src "$CTS_ROOT" --config "$CFG"
  preview-runner promote  --task static-init-a --lane A
  preview-runner deploy   --task static-init-b --lane B --src "$CTS_ROOT" --config "$CFG"
  preview-runner promote  --task static-init-b --lane B
fi

log "running doctor:"
PREVIEW_ENV=/etc/magestic-preview/preview.env preview-runner doctor

cat >&2 <<EOF

[bootstrap] DONE.
  Static lane A (main/pre-prod): http://${PUBLIC_HOST}:13001
  Static lane B (test):          http://${PUBLIC_HOST}:13002
Next: register a 'preview-host' server in MagesticAI (Settings > Servers):
  host=${PUBLIC_HOST}  auth=key  deploys.preview=/usr/local/bin/preview-runner
  host_path_map={"/home/magesticai/projects":"/home/saya/projects"}
EOF
