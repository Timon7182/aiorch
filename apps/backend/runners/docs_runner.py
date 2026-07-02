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


# Placeholders in doc_generator.md → manifest keys in a doc template's
# template.json. A template supplies the structure contract, the mkdocs.yml
# skeleton, the per-page templates, and optional extra instructions.
_TEMPLATE_PLACEHOLDERS: dict[str, str] = {
    "structure": "{{TEMPLATE_STRUCTURE}}",
    "mkdocs_yml": "{{TEMPLATE_MKDOCS_YML}}",
    "page_templates": "{{TEMPLATE_PAGE_TEMPLATES}}",
    "extra_instructions": "{{TEMPLATE_EXTRA}}",
}


def _template_search_dirs(project_dir: Path, name: str) -> list[Path]:
    """Resolution order: project override → user global → bundled built-in."""
    return [
        project_dir / ".magestic-ai" / "doc-templates" / name,
        Path.home() / ".magestic-ai" / "doc-templates" / name,
        _BACKEND_DIR / "prompts" / "doc_templates" / name,
    ]


def _load_template_manifest(project_dir: Path, name: str) -> dict | None:
    """Load the first `template.json` found for ``name``, or None."""
    import json

    for d in _template_search_dirs(project_dir, name):
        manifest = d / "template.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
                print(
                    f"[docs] template {name!r} manifest is not an object",
                    file=sys.stderr,
                )
                return None
            except (json.JSONDecodeError, OSError) as exc:
                print(
                    f"[docs] template {name!r} manifest unreadable: {exc}",
                    file=sys.stderr,
                )
                return None
    return None


def _resolve_template(project_dir: Path, name: str) -> dict:
    """Resolve a template manifest, falling back to 'default' when missing."""
    data = _load_template_manifest(project_dir, name)
    if data is None and name != "default":
        print(
            f"[docs] template {name!r} not found; falling back to 'default'",
            file=sys.stderr,
        )
        data = _load_template_manifest(project_dir, "default")
    return data or {}


def _apply_template(prompt_text: str, tpl: dict) -> str:
    """Substitute the {{TEMPLATE_*}} placeholders with the template's content."""
    for key, placeholder in _TEMPLATE_PLACEHOLDERS.items():
        value = tpl.get(key, "") if isinstance(tpl, dict) else ""
        prompt_text = prompt_text.replace(placeholder, str(value or ""))
    return prompt_text


async def _run(project_dir: Path, model: str, template: str = "default") -> int:
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

    # Fill the template placeholders from the selected doc template so the
    # generated site follows the chosen structure/mkdocs/page layout.
    tpl = _resolve_template(project_dir, template)
    prompt_text = _apply_template(prompt_text, tpl)
    print(f"[docs] using template '{template}'", flush=True)

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
        "--template",
        default=os.environ.get("DOCS_TEMPLATE", "default"),
        help="Doc template name to drive structure/mkdocs/page layout "
        "(default: 'default'). Resolved from the project's "
        ".magestic-ai/doc-templates/, then ~/.magestic-ai/doc-templates/, "
        "then the bundled built-ins.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args.project_dir, args.model, args.template))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        import traceback
        print(f"[docs] runner crashed: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
