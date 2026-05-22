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
        BITBUCKET_HOST="${BITBUCKET_HOST:-bitbucket.org}" \
        BITBUCKET_USERNAME="${BITBUCKET_USERNAME:-x-token-auth}" \
        BITBUCKET_TOKEN="${BITBUCKET_TOKEN:-${BITBUCKET_APP_PASSWORD:-}}" \
    bash <<'GIT_SETUP'
set -eu
CREDS=/home/magesticai/.git-credentials
: > "$CREDS"
chmod 600 "$CREDS"
[ -n "${GH_TOKEN:-}" ] && echo "https://oauth2:${GH_TOKEN}@github.com" >> "$CREDS"
[ -n "${GITLAB_TOKEN:-}" ] && echo "https://oauth2:${GITLAB_TOKEN}@gitlab.com" >> "$CREDS"
if [ -n "${BITBUCKET_TOKEN:-}" ]; then
    echo "https://${BITBUCKET_USERNAME}:${BITBUCKET_TOKEN}@${BITBUCKET_HOST}" >> "$CREDS"
fi
git config --global credential.helper "store --file=$CREDS"
GIT_SETUP

# Drop to non-root user (gosu handles signals properly for PID 1)
exec gosu magesticai "$@"
