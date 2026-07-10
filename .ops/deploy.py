"""Deploy MagesticAI to the LAN server.

Idempotent:
- Clones (or fast-forwards) the repo on the server.
- Uploads the bridge-mode compose file.
- Creates host data dirs.
- Builds + starts the container.
- Waits for /api/health.
- Extracts and prints the API token.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from remote import Remote, Result

REMOTE_REPO = "/home/saya/magestic"
REMOTE_COMPOSE = f"{REMOTE_REPO}/.ops/compose.server.yml"
COMPOSE_PROJECT = "magesticai-server"

# Single source of truth: the fork. All changes + deploys flow through here.
GIT_URL = "https://github.com/Timon7182/aiorch.git"
GIT_BRANCH = "main"


def ensure_repo(r: Remote) -> None:
    res = r.run(f"test -d {REMOTE_REPO}/.git && echo EXISTS || echo MISSING")
    if "EXISTS" in res.stdout:
        print(f"[deploy] repo exists; fetching latest {GIT_BRANCH}")
        # Make sure origin points at the single canonical repo, then hard-reset.
        r.run(f"cd {REMOTE_REPO} && git remote set-url origin {GIT_URL}", timeout=30, check=True)
        r.run(
            f"cd {REMOTE_REPO} && git fetch origin {GIT_BRANCH} && git reset --hard origin/{GIT_BRANCH}",
            timeout=120, check=True,
        )
    else:
        print(f"[deploy] cloning {GIT_URL} ({GIT_BRANCH})")
        r.run(
            f"git clone --depth 30 -b {GIT_BRANCH} {GIT_URL} {REMOTE_REPO}",
            timeout=180,
            check=True,
        )


def ensure_data_dirs(r: Remote) -> None:
    print("[deploy] ensuring host data + projects dirs")
    r.run("mkdir -p /home/saya/magestic-data /home/saya/projects", check=True)


def upload_override(r: Remote) -> None:
    print("[deploy] uploading bridge-mode compose override")
    local = Path(__file__).parent / "compose.server.yml"
    r.run(f"mkdir -p {REMOTE_REPO}/.ops")
    r.put_text(REMOTE_COMPOSE, local.read_text(encoding="utf-8"))


def _safe_print(text: str) -> None:
    sys.stdout.write(text.encode("utf-8", errors="replace").decode("utf-8") + "\n")


def build_and_up(r: Remote) -> None:
    print("[deploy] building image (this may take a few minutes)")
    res = r.run(
        f"cd {REMOTE_REPO} && docker compose -p {COMPOSE_PROJECT} -f {REMOTE_COMPOSE} build 2>&1 | tail -40",
        timeout=900,
    )
    _safe_print(res.stdout)
    if res.exit_code != 0:
        raise RuntimeError(f"build failed (exit {res.exit_code})")

    print("[deploy] starting container")
    res = r.run(
        f"cd {REMOTE_REPO} && docker compose -p {COMPOSE_PROJECT} -f {REMOTE_COMPOSE} up -d 2>&1 | tail -20",
        timeout=120,
    )
    _safe_print(res.stdout)
    if res.exit_code != 0:
        raise RuntimeError(f"up failed (exit {res.exit_code})")


def wait_healthy(r: Remote, deadline_s: int = 120) -> None:
    print("[deploy] waiting for /api/health")
    end = time.time() + deadline_s
    last = ""
    while time.time() < end:
        res = r.run("curl -sf -o /dev/null -w '%{http_code}' http://127.0.0.1:3101/api/health 2>&1 || echo curl-fail")
        last = res.stdout.strip()
        if last == "200":
            print(f"[deploy] healthy after {int(deadline_s - (end - time.time()))}s")
            return
        time.sleep(3)
    res = r.run(f"docker compose -p {COMPOSE_PROJECT} -f {REMOTE_COMPOSE} logs --tail=80 app 2>&1")
    print("[deploy] FAILED waiting for healthy; last status:", last)
    print(res.stdout)
    raise RuntimeError("container did not become healthy")


def show_token_and_status(r: Remote) -> None:
    print("\n=== TOKEN ===")
    res = r.run("docker exec magesticai cat /home/magesticai/.magestic-ai/.token 2>/dev/null || echo '(token not yet written)'")
    print(res.stdout.strip())
    print("\n=== CONTAINER STATUS ===")
    res = r.run("docker ps --filter name=magesticai --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
    print(res.stdout)


def main() -> int:
    r = Remote.from_env()
    try:
        ensure_repo(r)
        ensure_data_dirs(r)
        upload_override(r)
        build_and_up(r)
        wait_healthy(r)
        show_token_and_status(r)
        print("\n[deploy] DONE. Reach the UI at http://192.168.88.55:3101")
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
