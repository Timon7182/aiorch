"""Probe server network state for MagesticAI deployment planning."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from remote import Remote


def main() -> int:
    r = Remote.from_env()
    try:
        for label, cmd in [
            ("network interfaces", "ip -br link"),
            ("ip addresses", "ip -br addr"),
            ("listening ports near MagesticAI", "ss -tln | grep -E ':(3100|3101|3102|31[0-9][0-9]|8080|8081|22)\\b' || ss -tln | head -20"),
            ("docker version", "docker version --format '{{.Server.Version}}'"),
            ("docker networks", "docker network ls"),
            ("k3s/k8s state", "systemctl is-active k3s.service 2>/dev/null || echo 'not-active'; systemctl is-active kubelet.service 2>/dev/null || echo 'not-active'"),
            ("free disk under /home", "df -h /home"),
            ("docker disk usage", "docker system df 2>&1 | head -10"),
            ("existing magestic dir", "test -d /home/saya/magestic && echo EXISTS || echo MISSING"),
            ("ufw / iptables", "command -v ufw && ufw status 2>/dev/null | head -10; iptables -L INPUT -n 2>/dev/null | head -5 || echo 'no iptables read'"),
        ]:
            r.echo(label, r.run(cmd, timeout=10))
        return 0
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
