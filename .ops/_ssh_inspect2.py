"""Dig deeper into why pubkey auth is rejected post-acceptance."""

from __future__ import annotations

import os
import sys

import paramiko


def main() -> int:
    password = os.environ.get("SSH_PASSWORD")
    if not password:
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
    cmds = [
        "cat /etc/ssh/sshd_config.d/99-archlinux.conf 2>/dev/null",
        "cat /etc/ssh/sshd_config.d/20-systemd-userdb.conf 2>/dev/null",
        "cat /etc/ssh/sshd_config.d/10-archiso.conf 2>/dev/null",
        "ls -la /home /home/saya",
        "wc -l ~/.ssh/authorized_keys",
        "cat -A ~/.ssh/authorized_keys | tail -1",
        "sshd -T 2>/dev/null | grep -Ei '(pubkey|authorizedkeys|password|allowusers|allowgroups|match|usepam|challenge)' | head -25 || echo 'cannot run sshd -T'",
        "journalctl -u sshd --since '1 minute ago' --no-pager 2>/dev/null | tail -10 || true",
    ]
    for cmd in cmds:
        _, o, e = c.exec_command(cmd, timeout=10)
        print(f"$ {cmd}")
        print(o.read().decode().rstrip())
        er = e.read().decode().rstrip()
        if er:
            print(f"STDERR: {er}")
        print("---")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
