"""Runs INSIDE the magesticai container: analyze + real ToolExecutor.Write.

No agent loop is invoked. We construct ProjectAnalyzer and ToolExecutor
directly and exercise them as a developer/script would.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def seed_demo(demo: Path) -> Path:
    demo.mkdir(parents=True, exist_ok=True)
    main_py = demo / "main.py"
    # Reset the demo file each run so re-runs are idempotent (the previous
    # version of this script was appending shout() repeatedly).
    main_py.write_text(
        "# Demo project for MagesticAI tooling smoke.\n\n"
        "def greet(name: str) -> str:\n"
        "    return f'hello, {name}'\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    print(greet('magestic'))\n",
        encoding="utf-8",
    )
    pyproject = demo / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'magestic-demo'\nversion = '0.0.1'\nrequires-python = '>=3.10'\n",
        encoding="utf-8",
    )
    return main_py


def run_analyzer(demo: Path) -> dict:
    sys.path.insert(0, "/home/projects/MagesticAI/apps/backend")
    try:
        from project.analyzer import ProjectAnalyzer  # type: ignore
    except Exception as exc:
        return {"error": f"import ProjectAnalyzer failed: {exc!r}"}
    try:
        analyzer = ProjectAnalyzer(project_dir=demo)
        profile = analyzer.analyze()
    except Exception as exc:
        return {"error": f"analyze() failed: {exc!r}"}

    if hasattr(profile, "to_dict"):
        try:
            return {"profile": profile.to_dict()}
        except Exception:
            pass
    keys = [k for k in dir(profile) if not k.startswith("_")]
    out: dict = {}
    for k in keys:
        v = getattr(profile, k)
        if callable(v):
            continue
        try:
            json.dumps(v, default=str)
            out[k] = v
        except TypeError:
            out[k] = repr(v)[:200]
    return {"profile": out}


async def _write_via_executor(demo: Path, main_py: Path) -> dict:
    sys.path.insert(0, "/home/projects/MagesticAI/apps/backend")
    from tools.executor import ToolExecutor  # type: ignore

    executor = ToolExecutor(working_dir=demo)
    new_content = main_py.read_text(encoding="utf-8") + (
        "\n\n"
        "def shout(name: str) -> str:\n"
        "    # Added by ToolExecutor.Write (no agent loop).\n"
        "    return greet(name).upper()\n"
    )
    result = await executor.execute("Write", {"file_path": str(main_py), "content": new_content})
    return {
        "via": "ToolExecutor.execute('Write', ...)",
        "is_error": getattr(result, "is_error", None),
        "content_preview": (getattr(result, "content", "") or "")[:300],
    }


def main() -> int:
    demo = Path("/home/magesticai/projects/magestic-demo")
    main_py = seed_demo(demo)

    print("=== BEFORE ===")
    print(main_py.read_text(encoding="utf-8"))

    print("\n=== ANALYZER (ProjectAnalyzer.analyze) ===")
    analyzed = run_analyzer(demo)
    print(json.dumps(analyzed, indent=2, default=str)[:2200])

    print("\n=== TOOL WRITE (ToolExecutor.execute, real signature, no agent loop) ===")
    try:
        write_result = asyncio.run(_write_via_executor(demo, main_py))
        print(json.dumps(write_result, indent=2, default=str)[:800])
    except Exception as exc:
        print(f"executor write FAILED: {exc!r}")
        return 1

    print("\n=== AFTER ===")
    print(main_py.read_text(encoding="utf-8"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
