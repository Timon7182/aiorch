"""Exercise npm + frontend scripts + Gemini explainer.

Read-only smoke for the parts of the stack the goal lists explicitly:
- 'check how npm works' → npm --version + frontend package.json scripts
- 'ask llm how works' → Gemini API call summarising the OpenAPI surface
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
from remote import Remote


def _safe_print(label: str, text: str, limit: int = 1500) -> None:
    print(f"\n=== {label} ===")
    print(text[:limit].encode("utf-8", errors="replace").decode("utf-8"))


def npm_check(r: Remote) -> None:
    res = r.run(
        "docker exec magesticai bash -lc 'npm --version && which node && node --version "
        "&& jq -r \".scripts | to_entries[] | \\\"  \\(.key): \\(.value)\\\"\" "
        "/home/projects/MagesticAI/apps/frontend-web/package.json 2>/dev/null || "
        "head -40 /home/projects/MagesticAI/apps/frontend-web/package.json'",
        timeout=20,
    )
    _safe_print("NPM CHECK (inside container)", res.stdout + res.stderr)


def gemini_explainer(r: Remote) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        _safe_print("GEMINI EXPLAINER", "(GEMINI_API_KEY not set; skipping)")
        return

    res = r.run(
        "curl -sf http://127.0.0.1:3101/openapi.json "
        "-H \"Authorization: Bearer $(docker exec magesticai cat /home/magesticai/.magestic-ai/.token)\"",
        timeout=20,
    )
    try:
        api = json.loads(res.stdout)
    except json.JSONDecodeError:
        _safe_print("GEMINI EXPLAINER", "could not fetch OpenAPI to feed Gemini")
        return

    paths = sorted(api.get("paths", {}).keys())
    grouped: dict[str, list[str]] = {}
    for p in paths:
        head = "/".join(p.split("/")[:3]) or p
        grouped.setdefault(head, []).append(p)
    summary = "\n".join(f"{k}  ({len(v)} routes)" for k, v in sorted(grouped.items()))

    prompt = (
        "You are a technical writer.\n"
        "Below is a summary of REST routes exposed by a system called MagesticAI "
        "(Kanban + multi-agent coding platform: Planner/Coder/QA agents, PTY terminal, "
        "Monaco editor, git worktrees, multi-provider LLMs, Graphiti memory).\n\n"
        "Routes by top-level group:\n"
        f"{summary}\n\n"
        "Explain in 5 short paragraphs how this system works end-to-end: "
        "(1) the data model, (2) the agent orchestration, (3) the editor/terminal layer, "
        "(4) the memory layer, (5) what the new /api/ext routes (servers, databases, "
        "transcripts, docs-index) add. Keep each paragraph to 3-4 sentences."
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash-exp:generateContent?key={api_key}"
    )
    body = json.dumps(
        {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200}}
    ).encode("utf-8")

    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _safe_print("GEMINI EXPLAINER", f"gemini call failed: {exc!r}")
        return

    text = ""
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        text = json.dumps(payload, indent=2)[:1200]

    _safe_print("GEMINI EXPLAINER (gemini-2.0-flash-exp)", text, limit=4000)


def probe_real_project(r: Remote) -> None:
    """Read-only inventory of candidate real projects on the server."""
    res = r.run(
        "for d in family_budget_bot ai-dev-control claude-see lang-graph-ui telegram-bots PlatBot; do "
        "if [ -d /home/saya/$d ]; then "
        "  size=$(du -sh /home/saya/$d 2>/dev/null | cut -f1); "
        "  count=$(find /home/saya/$d -type f 2>/dev/null | wc -l); "
        "  echo \"$d  size=$size  files=$count\"; "
        "fi; done",
        timeout=30,
    )
    _safe_print("CANDIDATE PROJECTS ON SERVER", res.stdout + res.stderr)


def main() -> int:
    r = Remote.from_env()
    try:
        npm_check(r)
        probe_real_project(r)
        gemini_explainer(r)
        return 0
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
