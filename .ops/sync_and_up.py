"""Sync my local extension files to the server, rebuild, bring up, wait healthy."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from remote import Remote

REMOTE_REPO = "/home/saya/magestic"
REMOTE_COMPOSE = f"{REMOTE_REPO}/.ops/compose.server.yml"
COMPOSE_PROJECT = "magesticai-server"

LOCAL_ROOT = Path(__file__).resolve().parents[1]

# Files we own (extension layer) that must be on the server before the next build.
FILES_TO_SYNC = [
    "apps/web-server/server/main.py",
    "apps/web-server/server/routes/extensions.py",
    "apps/web-server/server/routes/hermes.py",
    "apps/web-server/server/routes/project_ingest.py",
    "apps/web-server/server/services/ext_storage.py",
    "apps/web-server/server/services/ssh_service.py",
    "apps/web-server/server/services/db_service.py",
    "apps/web-server/server/services/transcripts_service.py",
    "apps/web-server/server/services/docs_index_service.py",
    "apps/web-server/server/services/hermes_service.py",
    "apps/web-server/requirements.txt",
    "apps/frontend-web/src/App.tsx",
    "apps/frontend-web/src/components/Sidebar.tsx",
    "apps/frontend-web/src/components/AddProjectModal.tsx",
    "apps/frontend-web/src/pages/LoginPage.tsx",
    "apps/frontend-web/src/pages/HermesPage.tsx",
    "apps/frontend-web/src/pages/MembersPage.tsx",
    "apps/frontend-web/src/pages/TranscriptsPage.tsx",
    "apps/frontend-web/src/stores/auth-store.ts",
    ".ops/compose.server.yml",
]


def _safe(text: str) -> None:
    sys.stdout.write(text.encode("utf-8", errors="replace").decode("utf-8") + "\n")


def main() -> int:
    r = Remote.from_env()
    try:
        for rel in FILES_TO_SYNC:
            local = LOCAL_ROOT / rel
            if not local.exists():
                print(f"[sync] MISSING local file: {local}")
                continue
            remote = f"{REMOTE_REPO}/{rel}"
            content = local.read_text(encoding="utf-8")
            r.put_text(remote, content)
            print(f"[sync] {rel}  ({len(content)} bytes)")

        print("\n[build] running docker compose build (cached layers should be fast)")
        res = r.run(
            f"cd {REMOTE_REPO} && docker compose -p {COMPOSE_PROJECT} -f {REMOTE_COMPOSE} build 2>&1 | tail -50",
            timeout=900,
        )
        _safe(res.stdout)
        if res.exit_code != 0:
            raise RuntimeError(f"build failed (exit {res.exit_code})")

        print("\n[up] starting container")
        gemini_inline = ""
        gem = os.environ.get("GEMINI_API_KEY") or ""
        if gem:
            gemini_inline = f"GEMINI_API_KEY='{gem}' "
        res = r.run(
            f"cd {REMOTE_REPO} && {gemini_inline}docker compose -p {COMPOSE_PROJECT} -f {REMOTE_COMPOSE} up -d --force-recreate 2>&1 | tail -30",
            timeout=120,
        )
        _safe(res.stdout)
        if res.exit_code != 0:
            raise RuntimeError(f"up failed (exit {res.exit_code})")

        print("\n[wait] polling /api/health")
        end = time.time() + 120
        last_status = ""
        while time.time() < end:
            res = r.run(
                "curl -sf -o /dev/null -w '%{http_code}' http://127.0.0.1:3101/api/health 2>&1 || echo curl-fail"
            )
            last_status = res.stdout.strip()
            if last_status == "200":
                print(f"[wait] healthy")
                break
            time.sleep(3)
        else:
            print(f"[wait] not healthy yet (last: {last_status}); dumping logs:")
            res = r.run(
                f"docker compose -p {COMPOSE_PROJECT} -f {REMOTE_COMPOSE} logs --tail=80 app 2>&1"
            )
            _safe(res.stdout)
            return 1

        print("\n=== TOKEN ===")
        res = r.run("docker exec magesticai cat /home/magesticai/.magestic-ai/.token 2>/dev/null || echo '(token not yet written)'")
        print(res.stdout.strip())

        print("\n=== STATUS ===")
        res = r.run("docker ps --filter name=magesticai --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
        print(res.stdout)

        print("\n=== EXTENSION ENDPOINTS ===")
        res = r.run(
            "curl -sf http://127.0.0.1:3101/openapi.json 2>/dev/null | "
            "python -c 'import json, sys; d=json.load(sys.stdin); paths=[p for p in d[\"paths\"] if \"/api/ext\" in p]; print(\"\\n\".join(sorted(paths)) or \"(none — extensions not wired)\")'"
        )
        _safe(res.stdout)

        print("\n[done] Reach the UI at http://192.168.88.55:3101")
        return 0
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
