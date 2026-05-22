"""One-shot helper: authenticate with password, install local pubkey, probe server.

Intentionally private to this workspace. The password lives only in the SSH_PASSWORD
env var at runtime — do not commit it anywhere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko


def main() -> int:
    host = os.environ.get("SSH_HOST", "192.168.88.55")
    user = os.environ.get("SSH_USER", "saya")
    password = os.environ.get("SSH_PASSWORD")
    if not password:
        print("SSH_PASSWORD env var is required", file=sys.stderr)
        return 2

    pubkey = Path.home() / ".ssh" / "id_ed25519.pub"
    if not pubkey.exists():
        print(f"missing pubkey at {pubkey}", file=sys.stderr)
        return 2
    pubkey_text = pubkey.read_text(encoding="utf-8").strip()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
    )

    install_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"grep -qxF '{pubkey_text}' ~/.ssh/authorized_keys 2>/dev/null || "
        f"(echo '{pubkey_text}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys) && "
        "echo INSTALLED"
    )
    _, stdout, stderr = client.exec_command(install_cmd, timeout=30)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    print("# pubkey install:", out or "(no output)", err and f"err={err}" or "")

    probe = (
        "uname -a && echo --- && cat /etc/os-release | grep -E '^(NAME|VERSION)=' && echo --- "
        "&& free -h && echo --- && df -h / && echo --- && "
        "command -v docker && docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'no docker' && echo --- && "
        "command -v docker-compose && docker-compose version 2>/dev/null || (docker compose version 2>/dev/null || echo 'no compose') && echo --- && "
        "command -v git && git --version && echo --- && "
        "command -v python3 && python3 --version && echo --- && "
        "ip -br addr 2>/dev/null | head -5 && echo --- && "
        "ip route 2>/dev/null | head -3"
    )
    _, stdout, stderr = client.exec_command(probe, timeout=30)
    print("\n=== PROBE STDOUT ===\n" + stdout.read().decode())
    err = stderr.read().decode().strip()
    if err:
        print("\n=== PROBE STDERR ===\n" + err)

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
