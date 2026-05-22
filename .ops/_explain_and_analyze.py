"""Ask Gemini to explain the deployed system + run ProjectAnalyzer on a real project."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
from remote import Remote


GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash",
]


def call_gemini(prompt: str, api_key: str) -> tuple[str, str | None]:
    body = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1400},
        }
    ).encode("utf-8")
    last_err: str | None = None
    for model in GEMINI_MODELS:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = Request(url, data=body, method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=40) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            try:
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
                return text, model
            except (KeyError, IndexError, TypeError):
                return json.dumps(payload, indent=2)[:1500], model
        except urllib.error.HTTPError as exc:
            last_err = f"{model}: HTTP {exc.code}"
            continue
        except Exception as exc:
            last_err = f"{model}: {exc!r}"
            continue
    return f"(all candidate models failed; last error: {last_err})", None


def gemini_explainer(r: Remote, key: str) -> None:
    res = r.run(
        "curl -sf http://127.0.0.1:3101/openapi.json "
        "-H \"Authorization: Bearer $(docker exec magesticai cat /home/magesticai/.magestic-ai/.token)\"",
        timeout=20,
    )
    try:
        api = json.loads(res.stdout)
    except json.JSONDecodeError:
        print("could not load OpenAPI surface")
        return

    paths = sorted(api.get("paths", {}).keys())
    grouped: dict[str, list[str]] = {}
    for p in paths:
        head = "/".join(p.split("/")[:3]) or p
        grouped.setdefault(head, []).append(p)
    summary = "\n".join(f"{k}  ({len(v)} routes)" for k, v in sorted(grouped.items()))

    prompt = (
        "You are a senior staff engineer onboarding a new colleague.\n"
        "Below is a list of REST route groups exposed by MagesticAI — a Kanban + "
        "multi-agent coding platform with Planner/Coder/QA agents, PTY terminal, "
        "Monaco editor, git worktree isolation, multi-provider LLM support "
        "(Claude/Codex/Gemini/Ollama), and Graphiti knowledge-graph memory.\n\n"
        f"Route groups:\n{summary}\n\n"
        "Explain in 5 paragraphs (3-4 sentences each) how this system works end-to-end:\n"
        "1) Data model (projects, tasks, runs, files).\n"
        "2) Agent orchestration (Planner -> Coder -> QA -> the kanban moves).\n"
        "3) The editor + terminal + PTY layer for live coding.\n"
        "4) The memory layer (Graphiti, audit, logs).\n"
        "5) What the new /api/ext routes add (servers, databases, transcripts, docs-index) "
        "and how a team would use them day-to-day.\n"
    )

    print("\n=== GEMINI EXPLAINER ===")
    text, model = call_gemini(prompt, key)
    if model:
        print(f"[model: {model}]\n")
    sys.stdout.write(text.encode("utf-8", errors="replace").decode("utf-8") + "\n")


CONTAINER_ANALYZE = r"""
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, "/home/projects/MagesticAI/apps/backend")
from project.analyzer import ProjectAnalyzer  # type: ignore

project_path = Path("/home/magesticai/projects/family_budget_bot")
if not project_path.exists():
    print(json.dumps({"error": f"missing {project_path}"})); sys.exit(2)

analyzer = ProjectAnalyzer(project_dir=project_path)
profile = analyzer.analyze()

out = {}
for k in dir(profile):
    if k.startswith("_"): continue
    v = getattr(profile, k)
    if callable(v): continue
    try:
        json.dumps(v, default=str)
        out[k] = v
    except TypeError:
        out[k] = repr(v)[:200]
print(json.dumps(out, indent=2, default=str)[:3000])
"""


def analyze_real_project(r: Remote) -> None:
    print("\n=== ANALYZE REAL PROJECT: family_budget_bot ===")
    # The container has /home/saya/projects -> /home/magesticai/projects mounted ro? rw?
    # Let's first symlink the host's family_budget_bot into the container view, then run.
    r.run("ln -sfn /home/saya/family_budget_bot /home/saya/projects/family_budget_bot")

    r.put_text("/home/saya/projects/_run_analyze.py", CONTAINER_ANALYZE)
    res = r.run(
        "docker exec magesticai /home/projects/MagesticAI/.venv/bin/python "
        "/home/magesticai/projects/_run_analyze.py",
        timeout=120,
    )
    sys.stdout.write(res.stdout.encode("utf-8", errors="replace").decode("utf-8"))
    if res.stderr.strip():
        print("\nSTDERR:", res.stderr[:600])


def main() -> int:
    r = Remote.from_env()
    try:
        analyze_real_project(r)
        key = os.environ.get("GEMINI_API_KEY")
        if key:
            gemini_explainer(r, key)
        else:
            print("\n(GEMINI_API_KEY not set; explainer skipped)")
        return 0
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
