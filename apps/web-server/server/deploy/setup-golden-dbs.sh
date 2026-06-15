#!/usr/bin/env bash
#
# setup-golden-dbs.sh — build the 2 golden seed DBs (+ 2 static lane DBs) on the
# preview host from the cts pre-prod database.
#
# Run this ONCE (and re-run to refresh) on the preview host (192.168.88.55).
# It does NOT contain secrets — source creds come from env/args at run time.
#
# Result:
#   cts_golden_a  <- snapshot of pre-prod  (lane A: main/pre-prod previews clone this)
#   cts_golden_b  <- clone of golden_a     (lane B: test previews clone this)
#   cts_static_a  <- clone of golden_a     (persistent DB for the lane-A static deploy)
#   cts_static_b  <- clone of golden_b     (persistent DB for the lane-B static deploy)
#
# DEST (where the goldens are created) is read from /etc/magestic-preview/preview.env
# (PGHOST/PGPORT/PGUSER/PGPASSWORD/PGADMIN_DB and the PREVIEW_*_DB_* names).
#
# SOURCE (pre-prod) options — pick one:
#   A) Provide a dump file:        --dump /path/to/cts.dump   (custom -Fc, or .sql)
#   B) Dump over SSH (recommended): SRC_SSH=cargo-preprod SRC_DB=cts \
#        SRC_PG_CONTAINER=cts-db-postgres-1 SRC_PG_USER=cts ./setup-golden-dbs.sh
#   C) Dump from a reachable PG:    SRC_PGHOST=.. SRC_PGPORT=.. SRC_PGUSER=.. \
#        SRC_PGPASSWORD=.. SRC_DB=cts ./setup-golden-dbs.sh
#
# Optional: --sanitize /path/to/sanitize.sql   (run against golden_a after restore
#           to strip PII / truncate huge audit tables before it's cloned).

set -euo pipefail

PREVIEW_ENV="${PREVIEW_ENV:-/etc/magestic-preview/preview.env}"
[[ -f "$PREVIEW_ENV" ]] && { set -a; source "$PREVIEW_ENV"; set +a; }

GOLDEN_A="${PREVIEW_GOLDEN_DB_A:-cts_golden_a}"
GOLDEN_B="${PREVIEW_GOLDEN_DB_B:-cts_golden_b}"
STATIC_A="${PREVIEW_STATIC_DB_A:-cts_static_a}"
STATIC_B="${PREVIEW_STATIC_DB_B:-cts_static_b}"
ADMIN_DB="${PGADMIN_DB:-postgres}"

DUMP_FILE=""
SANITIZE_SQL=""
SRC_DB="${SRC_DB:-cts}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

log() { printf '[setup-golden-dbs] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing tool: $1"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)     DUMP_FILE="${2:?}"; shift 2;;
    --sanitize) SANITIZE_SQL="${2:?}"; shift 2;;
    *) die "unknown arg: $1";;
  esac
done

need psql; need createdb; need dropdb
[[ -n "${PGHOST:-}" ]] || die "DEST PGHOST not set (check $PREVIEW_ENV)"

# ---------------------------------------------------------------------------
# 1. Obtain a source dump (custom format) if not supplied.
# ---------------------------------------------------------------------------
if [[ -z "$DUMP_FILE" ]]; then
  DUMP_FILE="${WORKDIR}/src.dump"
  if [[ -n "${SRC_SSH:-}" ]]; then
    # Option B: dump inside the source's postgres container, over SSH.
    : "${SRC_PG_CONTAINER:?set SRC_PG_CONTAINER (e.g. cts-db-postgres-1)}"
    : "${SRC_PG_USER:?set SRC_PG_USER (the cts DB role)}"
    log "dumping $SRC_DB from $SRC_SSH:$SRC_PG_CONTAINER as $SRC_PG_USER"
    # shellcheck disable=SC2029
    ssh "$SRC_SSH" "docker exec -i $SRC_PG_CONTAINER pg_dump -U $SRC_PG_USER -Fc -d $SRC_DB" > "$DUMP_FILE"
  else
    # Option C: dump from a reachable Postgres using SRC_PG* env.
    need pg_dump
    : "${SRC_PGHOST:?set SRC_PGHOST or SRC_SSH}"
    : "${SRC_PGUSER:?set SRC_PGUSER}"
    log "dumping $SRC_DB from ${SRC_PGHOST}:${SRC_PGPORT:-5432} as ${SRC_PGUSER}"
    PGPASSWORD="${SRC_PGPASSWORD:-}" pg_dump \
      -h "$SRC_PGHOST" -p "${SRC_PGPORT:-5432}" -U "$SRC_PGUSER" \
      -Fc -d "$SRC_DB" -f "$DUMP_FILE"
  fi
fi
[[ -s "$DUMP_FILE" ]] || die "dump file is empty: $DUMP_FILE"
log "dump ready: $DUMP_FILE ($(du -h "$DUMP_FILE" | cut -f1))"

# ---------------------------------------------------------------------------
# helpers (DEST uses ambient PG* env)
# ---------------------------------------------------------------------------
admin() { psql -v ON_ERROR_STOP=1 -qX -d "$ADMIN_DB" "$@"; }

drop_db() {
  local db="$1"
  admin -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$db' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
  admin -c "DROP DATABASE IF EXISTS \"$db\";"
}

restore_into() {
  local db="$1" dump="$2"
  drop_db "$db"
  admin -c "CREATE DATABASE \"$db\";"
  case "$dump" in
    *.sql) psql -v ON_ERROR_STOP=1 -qX -d "$db" -f "$dump";;
    *)     need pg_restore; pg_restore --no-owner --no-privileges -d "$db" "$dump";;
  esac
}

clone_db() {  # CREATE DATABASE <new> TEMPLATE <src>
  local new="$1" src="$2"
  drop_db "$new"
  admin -c "CREATE DATABASE \"$new\" TEMPLATE \"$src\";"
}

# ---------------------------------------------------------------------------
# 2-5. Build golden_a, sanitize, then clone to golden_b / static_a / static_b
# ---------------------------------------------------------------------------
log "restoring -> $GOLDEN_A"
restore_into "$GOLDEN_A" "$DUMP_FILE"

if [[ -n "$SANITIZE_SQL" ]]; then
  [[ -f "$SANITIZE_SQL" ]] || die "sanitize sql not found: $SANITIZE_SQL"
  log "sanitizing $GOLDEN_A with $SANITIZE_SQL"
  psql -v ON_ERROR_STOP=1 -qX -d "$GOLDEN_A" -f "$SANITIZE_SQL"
fi

log "cloning $GOLDEN_A -> $GOLDEN_B"
clone_db "$GOLDEN_B" "$GOLDEN_A"
log "cloning $GOLDEN_A -> $STATIC_A"
clone_db "$STATIC_A" "$GOLDEN_A"
log "cloning $GOLDEN_B -> $STATIC_B"
clone_db "$STATIC_B" "$GOLDEN_B"

log "done. databases:"
admin -tAc "SELECT datname || '  ' || pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE datname IN ('$GOLDEN_A','$GOLDEN_B','$STATIC_A','$STATIC_B') ORDER BY datname;" >&2
