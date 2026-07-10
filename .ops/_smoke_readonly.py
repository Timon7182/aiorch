"""Read-only smoke-test of the deployed MagesticAI extension surface.

Performs no writes against the shared server. Just confirms endpoints exist,
require auth, and respond with empty collections when nothing has been stored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from remote import Remote


def main() -> int:
    r = Remote.from_env()
    try:
        token = r.run(
            "docker exec magesticai cat /home/magesticai/.magestic-ai/.token"
        ).stdout.strip()
        if not token:
            print("FAIL: no API token")
            return 1
        masked = token[:6] + "…" + token[-4:]
        print(f"token loaded: {masked}\n")

        def get(path: str, *, auth: bool = True) -> dict[str, Any]:
            url = f"http://127.0.0.1:3101{path}"
            cmd = "curl -sf"
            if auth:
                cmd += f" -H 'Authorization: Bearer {token}'"
            cmd += f" -w '\\n[http=%{{http_code}}]' {url}"
            res = r.run(cmd, timeout=15)
            return {"exit": res.exit_code, "body": res.stdout[:600], "stderr": res.stderr[:200]}

        # 1. Health endpoint (no auth)
        print("# GET /api/health  (no auth)")
        print(get("/api/health", auth=False))
        print()

        # 2. Auth-required: each /api/ext list/probe
        for path in (
            "/api/ext/servers",
            "/api/ext/databases",
            "/api/ext/transcripts/no-such-project",
            "/api/ext/docs-index/no-such-project/stats",
        ):
            print(f"# GET {path}")
            print(get(path))
            print()

        # 3. Confirm the ext routes are in the OpenAPI surface
        print("# OpenAPI ext-route inventory")
        cmd = (
            f"curl -sf -H 'Authorization: Bearer {token}' "
            f"http://127.0.0.1:3101/openapi.json"
        )
        res = r.run(cmd, timeout=20)
        if res.exit_code == 0 and res.stdout:
            data = json.loads(res.stdout)
            ext_paths = sorted(p for p in data.get("paths", {}) if "/api/ext" in p)
            for p in ext_paths:
                methods = sorted(data["paths"][p].keys())
                print(f"  {p:<48s} {methods}")
        else:
            print(f"  failed to fetch openapi: exit={res.exit_code}")

        return 0
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
