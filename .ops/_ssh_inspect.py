"""Inspect server-side SSH state to debug pubkey auth."""

from __future__ import annotations

import os
import sys

import paramiko


def main() -> int:
    password = os.environ.get("SSH_PASSWORD")
    if not password:
        print("SSH_PASSWORD required", file=sys.stderr)
        return 2

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        hostname="192.168.88.55",
        username="saya",
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
    )

    commands = [
        "ls -la ~/.ssh",
        "cat ~/.ssh/authorized_keys 2>/dev/null || echo NONE",
        "stat -c '%a %n' ~/.ssh ~/.ssh/authorized_keys 2>/dev/null",
        "getent passwd saya",
        "grep -Ei '(pubkey|authorized|allowusers|password)' /etc/ssh/sshd_config 2>/dev/null || echo 'cannot read'",
        "systemctl is-active sshd 2>/dev/null || true",
        "test -d /etc/ssh/sshd_config.d && ls /etc/ssh/sshd_config.d/ || true",
    ]
    for cmd in commands:
        _, o, e = c.exec_command(cmd, timeout=10)
        print(f"$ {cmd}")
        out = o.read().decode().rstrip()
        err = e.read().decode().rstrip()
        if out:
            print(out)
        if err:
            print(f"STDERR: {err}")
        print("---")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
