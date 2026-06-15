#!/usr/bin/env bash
#
# preview-runner.sh — generic, parameterized preview/deploy engine.
#
# This single script is the ONLY thing installed on a deploy host (it is
# allowlisted in the MagesticAI ServerProfile as profile.deploys["preview"]).
# Adding preview deploys for a *new* project requires NO new host script — the
# project just supplies a deploy.config.json; this runner consumes it.
#
# It is invoked over SSH by apps/web-server/server/services/preview_deploy_service.py
# via ssh_service.run_script(), which validates+quotes every argument. Do NOT add
# a free-form eval surface here.
#
# Subcommands:
#   deploy   --task <slug> --lane <A|B> --src <abs worktree> --config <abs deploy.config.json> [--ref <sha>]
#   teardown --task <slug>
#   promote  --task <slug> --lane <A|B>
#   list
#   reap     --ttl-hours <n>
#   doctor
#
# Contract: human-readable logs go to STDERR; the LAST line of STDOUT is a single
# JSON object (or array for `list`) that the Python caller parses.
#
# Host configuration (secrets, golden DB names, domain) is read from an env file,
# NOT passed as arguments. Default: /etc/magestic-preview/preview.env
# (override with PREVIEW_ENV=/abs/path). See preview.env.example.

set -euo pipefail

# SSH non-login shells (paramiko exec_command) get a minimal PATH; ensure the
# usual locations for docker/compose/psql/jq/nginx are present.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

# ---------------------------------------------------------------------------
# Config / globals
# ---------------------------------------------------------------------------
PREVIEW_ENV="${PREVIEW_ENV:-/etc/magestic-preview/preview.env}"
if [[ -f "$PREVIEW_ENV" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$PREVIEW_ENV"; set +a
fi

STATE_DIR="${MAGESTIC_PREVIEW_STATE:-$HOME/.magestic-preview}"
PORT_MIN="${PREVIEW_PORT_MIN:-12000}"
PORT_MAX="${PREVIEW_PORT_MAX:-12999}"
PREVIEW_DOMAIN="${PREVIEW_DOMAIN:-}"            # e.g. preview.cts.local — empty => port-only URLs
NGINX_ENABLED="${PREVIEW_NGINX_ENABLED:-0}"     # 1 to write vhosts
NGINX_VHOST_DIR="${PREVIEW_NGINX_VHOST_DIR:-/etc/nginx/sites-enabled}"
PUBLIC_HOST="${PREVIEW_PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
GOLDEN_DB_A="${PREVIEW_GOLDEN_DB_A:-cts_golden_a}"
GOLDEN_DB_B="${PREVIEW_GOLDEN_DB_B:-cts_golden_b}"
STATIC_DB_A="${PREVIEW_STATIC_DB_A:-$GOLDEN_DB_A}"
STATIC_DB_B="${PREVIEW_STATIC_DB_B:-$GOLDEN_DB_B}"
MAX_PREVIEWS="${PREVIEW_MAX_CONCURRENT:-2}"
# Postgres connection for clone/drop comes from standard PG* env (PGHOST, PGPORT,
# PGUSER, PGPASSWORD) defined in preview.env. We never echo these.

log()  { printf '%s %s\n' "[preview-runner]" "$*" >&2; }
die()  { log "ERROR: $*"; emit_json "{\"ok\":false,\"error\":$(json_str "$*")}"; exit 1; }
emit_json() { printf '%s\n' "$1"; }   # final line on stdout

# Minimal JSON string escaper (handles quotes/backslashes/newlines).
json_str() {
  local s=${1//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\t'/\\t}
  printf '"%s"' "$s"
}

need() { command -v "$1" >/dev/null 2>&1 || die "required tool not found on host: $1"; }

# Strict validators (defense in depth — caller already validates).
valid_slug() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "invalid slug: $1"; }
valid_lane() { [[ "$1" == "A" || "$1" == "B" ]] || die "invalid lane: $1"; }

golden_db_for() { [[ "$1" == "A" ]] && echo "$GOLDEN_DB_A" || echo "$GOLDEN_DB_B"; }
static_db_for() { [[ "$1" == "A" ]] && echo "$STATIC_DB_A" || echo "$STATIC_DB_B"; }
proj_name()     { echo "preview-$1"; }            # docker compose project name
static_proj()   { echo "cts-static-$(echo "$1" | tr 'A-Z' 'a-z')"; }
preview_db()    { echo "preview_${1//-/_}"; }     # postgres ident: dashes -> underscores

# Find a free TCP port in [PORT_MIN, PORT_MAX] not currently listening.
alloc_port() {
  local p
  for ((p=PORT_MIN; p<=PORT_MAX; p++)); do
    if ! ss -ltnH "sport = :$p" 2>/dev/null | grep -q .; then echo "$p"; return 0; fi
  done
  die "no free port in range $PORT_MIN-$PORT_MAX"
}

psql_admin() { PGDATABASE="${PGADMIN_DB:-postgres}" psql -v ON_ERROR_STOP=1 -qtAX "$@"; }

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
ARG_TASK=""; ARG_LANE=""; ARG_SRC=""; ARG_CONFIG=""; ARG_REF=""; ARG_TTL=""
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task)       ARG_TASK="${2:-}"; shift 2;;
      --lane)       ARG_LANE="${2:-}"; shift 2;;
      --src)        ARG_SRC="${2:-}"; shift 2;;
      --config)     ARG_CONFIG="${2:-}"; shift 2;;
      --ref)        ARG_REF="${2:-}"; shift 2;;
      --ttl-hours)  ARG_TTL="${2:-}"; shift 2;;
      *) die "unknown argument: $1";;
    esac
  done
}

# ---------------------------------------------------------------------------
# DB helpers (Postgres clone-from-golden / drop)
# ---------------------------------------------------------------------------
db_clone_from_golden() {
  local target="$1" golden="$2"
  [[ -z "${PGHOST:-}" ]] && { log "no PGHOST configured — skipping DB clone"; return 0; }
  need psql
  log "cloning DB $target from template $golden"
  # Drop a stale clone of the same name, then CREATE ... TEMPLATE (fast copy).
  psql_admin -c "DROP DATABASE IF EXISTS \"$target\";"
  psql_admin -c "CREATE DATABASE \"$target\" TEMPLATE \"$golden\";"
}

db_drop() {
  local target="$1"
  [[ -z "${PGHOST:-}" ]] && return 0
  command -v psql >/dev/null 2>&1 || return 0
  log "dropping DB $target"
  # Terminate connections so DROP succeeds.
  psql_admin -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$target' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
  psql_admin -c "DROP DATABASE IF EXISTS \"$target\";" || true
}

# ---------------------------------------------------------------------------
# Compose rendering — generated from deploy.config.json `components`.
# Each component runs its prebuilt image; the component marked "public" gets the
# allocated host port. DB connection is injected via env.
# ---------------------------------------------------------------------------
render_compose() {
  local task="$1" port="$2" dbname="$3" config="$4" out="$5"
  need jq
  local network="${PREVIEW_DOCKER_NETWORK:-bridge}"
  {
    echo "# generated by preview-runner.sh — do not edit"
    echo "services:"
    local n; n=$(jq '.components | length' "$config")
    for ((i=0; i<n; i++)); do
      local name comp_port public
      name=$(jq -r ".components[$i].name" "$config")
      comp_port=$(jq -r ".components[$i].port // 8080" "$config")
      public=$(jq -r ".components[$i].public // false" "$config")
      echo "  ${name}:"
      echo "    image: preview-${task}-${name}:latest"
      echo "    restart: unless-stopped"
      echo "    environment:"
      echo "      MAGESTIC_PREVIEW: \"${task}\""
      if [[ -n "${PGHOST:-}" ]]; then
        echo "      DB_HOST: \"${PREVIEW_DB_HOST_FOR_CONTAINERS:-$PGHOST}\""
        echo "      DB_PORT: \"${PGPORT:-5432}\""
        echo "      DB_NAME: \"${dbname}\""
        echo "      DB_USER: \"${PGUSER:-}\""
        echo "      DB_PASSWORD: \"${PGPASSWORD:-}\""
      fi
      # extra per-component env from config (object of string->string)
      jq -r ".components[$i].env // {} | to_entries[] | \"      \" + .key + \": \\\"\" + (.value|tostring) + \"\\\"\"" "$config"
      if [[ "$public" == "true" ]]; then
        echo "    ports:"
        echo "      - \"127.0.0.1:${port}:${comp_port}\""
      fi
    done
    echo "networks:"
    echo "  default:"
    echo "    name: ${network}"
    echo "    external: true"
  } > "$out"
}

build_images() {
  local task="$1" src="$2" config="$3"
  need docker; need jq
  local n; n=$(jq '.components | length' "$config")
  for ((i=0; i<n; i++)); do
    local name dockerfile context src_override root
    name=$(jq -r ".components[$i].name" "$config")
    dockerfile=$(jq -r ".components[$i].build.dockerfile" "$config")
    context=$(jq -r ".components[$i].build.context // \".\"" "$config")
    # Per-component source root override (absolute host path). Lets a split-repo
    # project (e.g. cts: backend in the task worktree, frontend in its own repo)
    # build each component from the right place. Defaults to --src.
    src_override=$(jq -r ".components[$i].build.src // \"\"" "$config")
    root="${src_override:-$src}"
    [[ -d "$root" ]] || die "build root not found for component $name: $root"
    log "building image preview-${task}-${name} (root=$root dockerfile=$dockerfile context=$context)"
    docker build -t "preview-${task}-${name}:latest" -f "${root}/${dockerfile}" "${root}/${context}" >&2
  done
}

# ---------------------------------------------------------------------------
# nginx vhost (optional)
# ---------------------------------------------------------------------------
write_vhost() {
  local task="$1" port="$2"
  [[ "$NGINX_ENABLED" != "1" || -z "$PREVIEW_DOMAIN" ]] && return 0
  need nginx
  local host="preview-${task}.${PREVIEW_DOMAIN}"
  local file="${NGINX_VHOST_DIR}/preview-${task}.conf"
  cat > "$file" <<EOF
# generated by preview-runner.sh for preview ${task}
server {
    listen 80;
    server_name ${host};
    location / {
        proxy_pass http://127.0.0.1:${port};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  if nginx -t >/dev/null 2>&1; then nginx -s reload >/dev/null 2>&1 || true; else log "nginx -t failed; leaving vhost but not reloading"; fi
  echo "http://${host}"
}

remove_vhost() {
  local task="$1"
  local file="${NGINX_VHOST_DIR}/preview-${task}.conf"
  [[ -f "$file" ]] || return 0
  rm -f "$file"
  command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1 && nginx -s reload >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
cmd_deploy() {
  valid_slug "$ARG_TASK"; valid_lane "$ARG_LANE"
  [[ -d "$ARG_SRC" ]] || die "src worktree not found: $ARG_SRC"
  [[ -f "$ARG_CONFIG" ]] || die "deploy config not found: $ARG_CONFIG"
  need docker; need jq

  # Concurrency cap: count live previews (state dirs) and refuse if at cap.
  local live; live=$(find "$STATE_DIR" -maxdepth 1 -type d -name 'preview-*' 2>/dev/null | wc -l | tr -d ' ')
  if (( live >= MAX_PREVIEWS )); then
    die "max concurrent previews reached ($live/$MAX_PREVIEWS) — stop one first or raise PREVIEW_MAX_CONCURRENT"
  fi

  local proj; proj=$(proj_name "$ARG_TASK")
  local sdir="${STATE_DIR}/${proj}"
  mkdir -p "$sdir"
  cp "$ARG_CONFIG" "${sdir}/deploy.config.json"   # promote reads it back later

  local dbstrat; dbstrat=$(jq -r '.services.postgres.strategy // "none"' "$ARG_CONFIG")
  local dbname=""
  if [[ "$dbstrat" == "clone" || "$dbstrat" == "ephemeral" ]]; then
    dbname=$(preview_db "$ARG_TASK")
    db_clone_from_golden "$dbname" "$(golden_db_for "$ARG_LANE")"
  fi

  build_images "$ARG_TASK" "$ARG_SRC" "$ARG_CONFIG"

  local port; port=$(alloc_port)
  local compose="${sdir}/docker-compose.yml"
  render_compose "$ARG_TASK" "$port" "$dbname" "$ARG_CONFIG" "$compose"

  log "starting compose project $proj on port $port"
  docker compose -p "$proj" -f "$compose" up -d >&2

  local vhost_url; vhost_url=$(write_vhost "$ARG_TASK" "$port" || true)
  local url="${vhost_url:-http://${PUBLIC_HOST}:${port}}"

  # Persist preview metadata (epoch for the reaper).
  local now; now=$(date +%s)
  cat > "${sdir}/meta.json" <<EOF
{"task":$(json_str "$ARG_TASK"),"lane":$(json_str "$ARG_LANE"),"port":$port,"url":$(json_str "$url"),"db":$(json_str "$dbname"),"created_at":$now,"ref":$(json_str "$ARG_REF")}
EOF

  emit_json "{\"ok\":true,\"task\":$(json_str "$ARG_TASK"),\"lane\":$(json_str "$ARG_LANE"),\"url\":$(json_str "$url"),\"ip\":$(json_str "$PUBLIC_HOST"),\"port\":$port,\"db\":$(json_str "$dbname")}"
}

cmd_teardown() {
  valid_slug "$ARG_TASK"
  local proj; proj=$(proj_name "$ARG_TASK")
  local sdir="${STATE_DIR}/${proj}"
  local compose="${sdir}/docker-compose.yml"
  if [[ -f "$compose" ]]; then
    log "tearing down compose project $proj"
    docker compose -p "$proj" -f "$compose" down -v --remove-orphans >&2 || true
  else
    docker compose -p "$proj" down -v --remove-orphans >&2 2>/dev/null || true
  fi
  # remove preview-specific images
  docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep "^preview-${ARG_TASK}-" | xargs -r docker rmi -f >&2 2>/dev/null || true
  db_drop "$(preview_db "$ARG_TASK")"
  remove_vhost "$ARG_TASK"
  rm -rf "$sdir"
  emit_json "{\"ok\":true,\"task\":$(json_str "$ARG_TASK"),\"status\":\"torn_down\"}"
}

cmd_promote() {
  valid_slug "$ARG_TASK"; valid_lane "$ARG_LANE"
  need docker; need jq
  local proj; proj=$(proj_name "$ARG_TASK")
  local sdir="${STATE_DIR}/${proj}"
  [[ -f "${sdir}/docker-compose.yml" ]] || die "no live preview to promote for $ARG_TASK"

  local static_proj; static_proj=$(static_proj "$ARG_LANE")
  local static_db; static_db=$(static_db_for "$ARG_LANE")
  local static_dir="${STATE_DIR}/${static_proj}"
  mkdir -p "$static_dir"

  # Re-tag the validated preview images as the static lane's images.
  local n; n=$(jq '.components | length' "${sdir}/deploy.config.json" 2>/dev/null || echo 0)
  # Render a static compose pinned to the lane's persistent DB and a stable port.
  local static_port; static_port="${PREVIEW_STATIC_PORT_A:-13001}"
  [[ "$ARG_LANE" == "B" ]] && static_port="${PREVIEW_STATIC_PORT_B:-13002}"

  # Retag images preview-<task>-<comp> -> static-<lane>-<comp>
  for img in $(docker images --format '{{.Repository}}' | grep "^preview-${ARG_TASK}-" | sort -u); do
    local comp="${img#preview-${ARG_TASK}-}"
    docker tag "${img}:latest" "static-$(echo "$ARG_LANE"|tr 'A-Z' 'a-z')-${comp}:latest" >&2
  done

  log "promoting preview $ARG_TASK -> static lane $ARG_LANE ($static_proj, db=$static_db, port=$static_port)"
  # Reuse the preview compose but point at the persistent static DB + stable port.
  local static_compose="${static_dir}/docker-compose.yml"
  sed -e "s/preview-${ARG_TASK}-/static-$(echo "$ARG_LANE"|tr 'A-Z' 'a-z')-/g" \
      -e "s/127.0.0.1:[0-9]*:/127.0.0.1:${static_port}:/g" \
      "${sdir}/docker-compose.yml" > "$static_compose"
  # swap DB name to the persistent lane DB
  if [[ -n "${PGHOST:-}" ]]; then
    sed -i "s/DB_NAME: \".*\"/DB_NAME: \"${static_db}\"/g" "$static_compose"
  fi
  docker compose -p "$static_proj" -f "$static_compose" up -d >&2

  local static_url="http://${PUBLIC_HOST}:${static_port}"
  cat > "${static_dir}/meta.json" <<EOF
{"lane":$(json_str "$ARG_LANE"),"port":$static_port,"url":$(json_str "$static_url"),"db":$(json_str "$static_db"),"promoted_from":$(json_str "$ARG_TASK"),"created_at":$(date +%s)}
EOF

  # Tear the ephemeral preview down now that it's promoted.
  cmd_teardown_quiet "$ARG_TASK"

  emit_json "{\"ok\":true,\"task\":$(json_str "$ARG_TASK"),\"lane\":$(json_str "$ARG_LANE"),\"static\":true,\"url\":$(json_str "$static_url"),\"port\":$static_port,\"db\":$(json_str "$static_db")}"
}

cmd_teardown_quiet() {
  local task="$1"
  local proj; proj=$(proj_name "$task")
  local sdir="${STATE_DIR}/${proj}"
  [[ -f "${sdir}/docker-compose.yml" ]] && docker compose -p "$proj" -f "${sdir}/docker-compose.yml" down -v --remove-orphans >&2 2>/dev/null || true
  db_drop "$(preview_db "$task")"
  remove_vhost "$task"
  rm -rf "$sdir"
}

cmd_list() {
  local out="["; local first=1
  if [[ -d "$STATE_DIR" ]]; then
    for d in "$STATE_DIR"/preview-*; do
      [[ -f "$d/meta.json" ]] || continue
      [[ $first -eq 1 ]] && first=0 || out+=","
      out+=$(cat "$d/meta.json")
    done
  fi
  out+="]"
  emit_json "$out"
}

cmd_reap() {
  local ttl="${ARG_TTL:-}"
  [[ "$ttl" =~ ^[0-9]+$ ]] || die "invalid --ttl-hours: $ttl"
  local cutoff; cutoff=$(( $(date +%s) - ttl*3600 ))
  local reaped=""; local first=1
  if [[ -d "$STATE_DIR" ]]; then
    for d in "$STATE_DIR"/preview-*; do
      [[ -f "$d/meta.json" ]] || continue
      local created task
      created=$(jq -r '.created_at // 0' "$d/meta.json" 2>/dev/null || echo 0)
      task=$(jq -r '.task // ""' "$d/meta.json" 2>/dev/null || echo "")
      if [[ -n "$task" && "$created" -lt "$cutoff" ]]; then
        log "reaping expired preview $task (created=$created cutoff=$cutoff)"
        cmd_teardown_quiet "$task"
        [[ $first -eq 1 ]] && first=0 || reaped+=","
        reaped+=$(json_str "$task")
      fi
    done
  fi
  emit_json "{\"ok\":true,\"reaped\":[${reaped}]}"
}

cmd_doctor() {
  local docker_ok jq_ok psql_ok nginx_ok db_ok
  command -v docker >/dev/null 2>&1 && docker_ok=true || docker_ok=false
  command -v jq >/dev/null 2>&1 && jq_ok=true || jq_ok=false
  command -v psql >/dev/null 2>&1 && psql_ok=true || psql_ok=false
  command -v nginx >/dev/null 2>&1 && nginx_ok=true || nginx_ok=false
  db_ok=false
  if [[ -n "${PGHOST:-}" ]] && command -v psql >/dev/null 2>&1; then
    psql_admin -c "SELECT 1;" >/dev/null 2>&1 && db_ok=true || db_ok=false
  fi
  emit_json "{\"ok\":true,\"docker\":$docker_ok,\"jq\":$jq_ok,\"psql\":$psql_ok,\"nginx\":$nginx_ok,\"db\":$db_ok,\"state_dir\":$(json_str "$STATE_DIR")}"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
  local sub="${1:-}"; shift || true
  mkdir -p "$STATE_DIR"
  case "$sub" in
    deploy)   parse_args "$@"; cmd_deploy;;
    teardown) parse_args "$@"; cmd_teardown;;
    promote)  parse_args "$@"; cmd_promote;;
    list)     cmd_list;;
    reap)     parse_args "$@"; cmd_reap;;
    doctor)   cmd_doctor;;
    *) die "unknown subcommand: ${sub:-<none>} (expected deploy|teardown|promote|list|reap|doctor)";;
  esac
}

main "$@"
