#!/usr/bin/env python3
"""Refresh /home/saya/.aiorch-secrets from /home/saya/.claude/.credentials.json.

Extracts the accessToken + refreshToken from the host's Claude credentials
file (kept refreshed by the host's `claude` CLI) and writes them into the
deploy_local.sh-sourced secrets file as:

    export CLAUDE_CODE_OAUTH_TOKEN=...
    export CLAUDE_CODE_OAUTH_REFRESH_TOKEN=...

After running this, restart the container so the env vars are re-read.

Idempotent: overwrites any existing CLAUDE_CODE_OAUTH_* lines.
"""

import json
import os
import sys
from pathlib import Path

CREDS_PATH = Path("/home/saya/.claude/.credentials.json")
SECRETS_PATH = Path("/home/saya/.aiorch-secrets")


def main() -> int:
    if not CREDS_PATH.exists():
        print(f"missing: {CREDS_PATH}", file=sys.stderr)
        return 1

    creds = json.loads(CREDS_PATH.read_text())
    oauth = creds.get("claudeAiOauth", {})
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    if not access or not refresh:
        print("credentials.json missing accessToken or refreshToken", file=sys.stderr)
        return 2

    print(f"accessToken starts with: {access[:25]}...")
    print(f"refreshToken starts with: {refresh[:25]}...")
    print(f"expiresAt: {oauth.get('expiresAt')}")

    # Rewrite the secrets file with the two OAuth lines kept up to date.
    lines: list[str] = []
    if SECRETS_PATH.exists():
        for raw in SECRETS_PATH.read_text().splitlines():
            if raw.startswith("export CLAUDE_CODE_OAUTH_TOKEN="):
                continue
            if raw.startswith("export CLAUDE_CODE_OAUTH_REFRESH_TOKEN="):
                continue
            lines.append(raw)

    lines.append(f"export CLAUDE_CODE_OAUTH_TOKEN={access}")
    lines.append(f"export CLAUDE_CODE_OAUTH_REFRESH_TOKEN={refresh}")

    # umask 077 + chmod 600 — secrets file should not be world-readable.
    old_umask = os.umask(0o077)
    try:
        SECRETS_PATH.write_text("\n".join(lines) + "\n")
        SECRETS_PATH.chmod(0o600)
    finally:
        os.umask(old_umask)

    print(f"wrote {SECRETS_PATH} (mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
