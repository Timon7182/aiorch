"""Run the in-container demo: analyze + code-level Write (no agent loop)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from remote import Remote


def main() -> int:
    r = Remote.from_env()
    try:
        local_script = Path(__file__).parent / "demo_inside.py"
        if not local_script.exists():
            print(f"missing {local_script}")
            return 1

        # The container bind-mounts /home/saya/projects -> /home/magesticai/projects (rw),
        # so writing to the host-side path makes the script visible inside.
        r.put_text("/home/saya/projects/_demo_run.py", local_script.read_text(encoding="utf-8"))

        res = r.run(
            "docker exec magesticai /home/projects/MagesticAI/.venv/bin/python "
            "/home/magesticai/projects/_demo_run.py",
            timeout=180,
        )
        sys.stdout.write(res.stdout.encode("utf-8", errors="replace").decode("utf-8"))
        if res.stderr.strip():
            sys.stderr.write("\nSTDERR:\n" + res.stderr.encode("utf-8", errors="replace").decode("utf-8"))

        print("\n[host] confirming on-disk via host shell:")
        for cmd in (
            "ls -la /home/saya/projects/magestic-demo",
            "cat /home/saya/projects/magestic-demo/main.py",
        ):
            cr = r.run(cmd)
            print(f"$ {cmd}")
            print(cr.stdout)

        return res.exit_code
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
