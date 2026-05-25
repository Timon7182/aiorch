#!/bin/bash
set -euo pipefail

# =============================================================================
# MagesticAI Docker Entrypoint
# =============================================================================
# Runs as root to set up iptables firewall blocking LAN access,
# then drops to the magesticai user via gosu.
# =============================================================================

GATEWAY_IP="${CONTAINER_GATEWAY:?CONTAINER_GATEWAY is required (set via docker-compose .env LAN_GATEWAY)}"
BLOCKED_RANGES="${CONTAINER_BLOCKED_RANGES:-10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
ENABLE_LAN_FIREWALL="${CONTAINER_LAN_FIREWALL:-true}"

if [ "$ENABLE_LAN_FIREWALL" = "true" ]; then
    echo "[entrypoint] Setting up LAN firewall..."

    iptables -F OUTPUT

    # Allow loopback + established return traffic
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

    # Allow DNS + ICMP to gateway (needed for internet routing)
    iptables -A OUTPUT -d "$GATEWAY_IP" -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -d "$GATEWAY_IP" -p tcp --dport 53 -j ACCEPT
    iptables -A OUTPUT -d "$GATEWAY_IP" -p icmp -j ACCEPT

    # Block all RFC 1918 private ranges
    IFS=',' read -ra RANGES <<< "$BLOCKED_RANGES"
    for range in "${RANGES[@]}"; do
        iptables -A OUTPUT -d "$range" -j DROP
        echo "[entrypoint]   Blocked: $range"
    done

    echo "[entrypoint] LAN firewall active."
fi

# Claim ownership of the ~/.claude named volume — Docker creates it root-owned
# on first start, but the claude_token_service runs as the magesticai user and
# needs to write credentials.json there. Idempotent: subsequent starts are
# no-ops when the volume is already owned correctly.
if [ -d /home/magesticai/.claude ]; then
    chown -R magesticai:magesticai /home/magesticai/.claude
fi

# Restore ~/.claude.json across container recreates. The named volume only
# persists ~/.claude/ — the sibling ~/.claude.json (Claude CLI config/state)
# lives in the container's writable layer and is wiped by `--force-recreate`
# on every deploy. It can't be a bind/volume mount because the CLI rewrites it
# via atomic rename (rename-over-a-mount fails), so instead we restore a
# writable copy on start from the newest backup the CLI persists into the
# volume (~/.claude/backups/). No-op once the file is present.
CLAUDE_JSON=/home/magesticai/.claude.json
if [ ! -f "$CLAUDE_JSON" ] && [ -d /home/magesticai/.claude/backups ]; then
    NEWEST_BACKUP="$(ls -1t /home/magesticai/.claude/backups/.claude.json.backup.* 2>/dev/null | head -n1 || true)"
    if [ -n "$NEWEST_BACKUP" ] && [ -f "$NEWEST_BACKUP" ]; then
        cp "$NEWEST_BACKUP" "$CLAUDE_JSON"
        chown magesticai:magesticai "$CLAUDE_JSON"
        echo "[entrypoint] restored ~/.claude.json from $(basename "$NEWEST_BACKUP")"
    fi
fi

# Ensure the CLI treats the container as already onboarded. The restored
# config (and the CLI's own backups) can lack `hasCompletedOnboarding`, which
# makes the interactive `claude` REPL re-run its login/onboarding wizard even
# though valid subscription credentials exist. Idempotent — only writes when
# the flag isn't already true.
if [ -f "$CLAUDE_JSON" ] && command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys
p=sys.argv[1]
try:
 d=json.load(open(p))
except Exception:
 sys.exit(0)
if d.get("hasCompletedOnboarding") is not True:
 d["hasCompletedOnboarding"]=True
 json.dump(d,open(p,"w"),indent=2)
 print("[entrypoint] set hasCompletedOnboarding=true in ~/.claude.json")' "$CLAUDE_JSON" || true
    chown magesticai:magesticai "$CLAUDE_JSON"
fi

# Configure git credentials from forwarded env vars so HTTPS clone/push works
# without prompting. Runs as the magesticai user; writes ~/.git-credentials
# (mode 600) and points git at the `store` helper.
#
# Bitbucket supports both Cloud (bitbucket.org + username + app password) and
# self-hosted Server (custom host + HTTP access token). BITBUCKET_HOST defaults
# to bitbucket.org. For Server tokens, leaving BITBUCKET_USERNAME unset uses
# "x-token-auth" as the placeholder username, which Bitbucket Server accepts.
gosu magesticai \
    env \
        GH_TOKEN="${GH_TOKEN:-}" \
        GITLAB_TOKEN="${GITLAB_TOKEN:-}" \
        BITBUCKET_HOST="${BITBUCKET_HOST:-}" \
        BITBUCKET_USERNAME="${BITBUCKET_USERNAME:-}" \
        BITBUCKET_TOKEN="${BITBUCKET_TOKEN:-${BITBUCKET_APP_PASSWORD:-}}" \
    bash <<'GIT_SETUP'
set -eu
CREDS=/home/magesticai/.git-credentials
: > "$CREDS"
chmod 600 "$CREDS"
[ -n "${GH_TOKEN:-}" ] && echo "https://oauth2:${GH_TOKEN}@github.com" >> "$CREDS"
[ -n "${GITLAB_TOKEN:-}" ] && echo "https://oauth2:${GITLAB_TOKEN}@gitlab.com" >> "$CREDS"
git config --global credential.helper "store --file=$CREDS"

# Bitbucket: Cloud uses USERNAME + APP_PASSWORD (basic auth, credential store).
# Self-hosted Server / older versions use HTTP Access Tokens with Bearer auth,
# which the credential store can't express — use http.<url>.extraheader instead.
if [ -n "${BITBUCKET_TOKEN:-}" ]; then
    if [ -n "${BITBUCKET_USERNAME:-}" ]; then
        HOST="${BITBUCKET_HOST:-bitbucket.org}"
        echo "https://${BITBUCKET_USERNAME}:${BITBUCKET_TOKEN}@${HOST}" >> "$CREDS"
    elif [ -n "${BITBUCKET_HOST:-}" ]; then
        AUTH_B64=$(printf 'Bearer %s' "$BITBUCKET_TOKEN")
        git config --global "http.https://${BITBUCKET_HOST}/.extraheader" "Authorization: ${AUTH_B64}"
    fi
fi
GIT_SETUP

# Drop to non-root user (gosu handles signals properly for PID 1)
exec gosu magesticai "$@"
