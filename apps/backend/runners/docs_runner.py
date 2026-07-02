#!/usr/bin/env python3
"""Documentation generator runner.

Spawned as a subprocess by the web server's `docs_generator_service`. Uses the
Claude Agent SDK (same path as spec_runner.py / run.py) so it inherits the
OAuth-token-via-env-var auth that the rest of the platform relies on, plus
the security profile, MCP integrations, and SDK message-parser patches.

Usage::

    python -u apps/backend/runners/docs_runner.py \\
        --project-dir /home/magesticai/projects/cts-backend

Exits 0 on success, non-zero on agent error.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Make `from core.client import ...` work whether we're invoked with cwd
# inside apps/backend or anywhere else.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Force UTF-8 stdout/stderr on Windows so the parent (FastAPI on Linux) sees
# correct bytes when it reads the stream. Harmless on POSIX.
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name)
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _read_changed_files(changed_files: Path | None) -> list[str]:
    """Read the newline-delimited changed-file list, if provided."""
    if changed_files is None:
        return []
    try:
        return [
            line.strip()
            for line in changed_files.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return []


def _incremental_prompt_section(changed: list[str]) -> str:
    """Prompt appendix instructing an incremental (changed-files-only) update."""
    # Cap the enumerated list so a huge diff doesn't blow the prompt budget.
    shown = changed[:200]
    listing = "\n".join(f"- {p}" for p in shown)
    more = "" if len(changed) <= len(shown) else f"\n- ...and {len(changed) - len(shown)} more"
    return (
        "\n\n---\n\n"
        "## INCREMENTAL UPDATE\n\n"
        "This is an **INCREMENTAL** update, not a full regeneration. Only these "
        "source files changed since the docs were last generated:\n\n"
        f"{listing}{more}\n\n"
        "Update ONLY the doc pages affected by these files. Do NOT regenerate "
        "unaffected pages, and do not rewrite hand-authored content. If none of "
        "the changes are doc-relevant, make no edits and exit.\n"
    )


async def _run(project_dir: Path, model: str, changed_files: Path | None = None) -> int:
    # Resolve through the prompt resolver so per-project overrides
    # (MAGESTIC_PROMPT_OVERRIDE_DIR) take precedence over the bundled default.
    from prompts_pkg.prompt_resolver import resolve_prompt_file

    prompt_path = resolve_prompt_file("doc_generator.md")
    if not prompt_path.is_file():
        print(f"[docs] prompt missing at {prompt_path}", file=sys.stderr)
        return 2

    if not project_dir.is_dir():
        print(f"[docs] project_dir not a directory: {project_dir}", file=sys.stderr)
        return 2

    prompt_text = prompt_path.read_text(encoding="utf-8")

    changed = _read_changed_files(changed_files)
    if changed:
        prompt_text = prompt_text + _incremental_prompt_section(changed)
        print(f"[docs] incremental mode: {len(changed)} changed file(s)", flush=True)

    # Lazy-import so the path manipulation above is in effect first.
    from core.client import create_client
    from core.error_utils import safe_receive_messages

    # `create_client` requires a spec_dir for its settings-file location. We
    # don't have a real spec, so use a docs-specific scratch directory under
    # the project's .magestic-ai/.
    spec_dir = project_dir / ".magestic-ai" / "docs-runner"
    spec_dir.mkdir(parents=True, exist_ok=True)

    # core.client writes ~/.claude/settings-headless.json on every call but
    # doesn't create the parent. In containers rebuilt without ~/.claude/
    # (no volume mount, fresh image), the directory is missing and the open
    # fails. Pre-create it here.
    claude_home = Path.home() / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)

    print(f"[docs] starting agent (model={model}) in {project_dir}", flush=True)

    client = create_client(
        project_dir=project_dir,
        spec_dir=spec_dir,
        model=model,
        agent_type="coder",  # closest fit: Read/Write/Glob/Bash for git rev-parse
        max_thinking_tokens=None,
    )

    # Open the SDK connection and pump the doc-generator prompt through.
    try:
        await client.connect()
    except AttributeError:
        # Older SDK builds expose connect via a different name; the SDK auto-
        # connects on first query() in that case, so safely ignore.
        pass

    tool_count = 0
    text_chars = 0
    try:
        await client.query(prompt_text)

        async for msg in safe_receive_messages(client, caller="docs_runner"):
            msg_type = type(msg).__name__
            if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                for block in msg.content:
                    block_type = type(block).__name__
                    if block_type == "TextBlock" and hasattr(block, "text"):
                        text_chars += len(block.text)
                        # Stream so the parent can read progress; flush eagerly.
                        sys.stdout.write(block.text)
                        sys.stdout.flush()
                    elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                        tool_count += 1
                        name = block.name
                        inp = getattr(block, "input", None) or {}
                        summary = ""
                        if isinstance(inp, dict):
                            for key in ("file_path", "path", "command", "pattern"):
                                v = inp.get(key)
                                if v:
                                    s = str(v)
                                    summary = s if len(s) <= 80 else s[:77] + "..."
                                    break
                        print(
                            f"\n[docs] tool#{tool_count} {name}({summary})",
                            flush=True,
                        )
            elif msg_type == "ResultMessage":
                # End-of-conversation marker from the SDK.
                break
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    print(
        f"\n[docs] agent done. tools_used={tool_count} text_chars={text_chars}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Documentation generator runner")
    parser.add_argument(
        "--project-dir",
        required=True,
        type=Path,
        help="Absolute path to the project directory to document",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DOCS_MODEL", "claude-sonnet-4-5-20250929"),
        help="Claude model to use for the doc agent (default: sonnet-4-5)",
    )
    parser.add_argument(
        "--changed-files",
        type=Path,
        default=None,
        help="Path to a newline-delimited list of source files changed since the "
             "docs were last generated. Triggers an incremental (affected-pages-"
             "only) update instead of a full regeneration.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args.project_dir, args.model, args.changed_files))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        import traceback
        print(f"[docs] runner crashed: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
