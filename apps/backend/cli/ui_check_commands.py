"""
UI Check Commands
=================

CLI command for on-demand browser UI verification (taskType == "ui_check").

Runs a single ``ui_checker`` agent session — no planner, no coder, no
worktree. The agent drives a real headless browser (Playwright MCP), executes
the user's check steps against the target URL, and writes:

- ``<spec_dir>/ui_check_report.md``   (human-readable report, exact contract)
- ``<spec_dir>/ui_check_result.json`` (machine-readable verdict)
- ``<spec_dir>/evidence-ui-check/``   (screenshots)

If the agent fails to produce the artifacts, this runner writes an honest
BLOCKED report itself — a UI check must never end without a verdict on disk.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure parent directory is in path for imports (before other imports)
_PARENT_DIR = Path(__file__).parent.parent
if str(_PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(_PARENT_DIR))

from core.client import create_client
from core.phase_event import ExecutionPhase, emit_phase
from debug import debug, debug_error, debug_section, debug_success
from phase_config import get_phase_model, get_phase_thinking_budget
from prompts_pkg import get_ui_check_prompt
from security.tool_input_validator import get_safe_tool_input
from task_logger import LogEntryType, LogPhase, get_task_logger

from .utils import print_banner, validate_environment

#: Verdicts the protocol allows (ui_check.md Step 5). Keep in sync.
UI_CHECK_VERDICTS = {
    "PASS",
    "FAIL",
    "BUG_CONFIRMED",
    "BUG_NOT_REPRODUCED",
    "BUG_INTERMITTENT",
    "FIX_CONFIRMED",
    "FIX_FAILED",
    "BLOCKED",
}


def read_ui_check_verdict(spec_dir: Path) -> str | None:
    """Return the verdict from ui_check_result.json, or None if absent/invalid."""
    result_file = spec_dir / "ui_check_result.json"
    if not result_file.is_file():
        return None
    try:
        verdict = (json.loads(result_file.read_text()) or {}).get("verdict")
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if isinstance(verdict, str) and verdict.upper() in UI_CHECK_VERDICTS:
        return verdict.upper()
    return None


def write_blocked_fallback(spec_dir: Path, reason: str) -> None:
    """Write a BLOCKED report/result when the agent produced none.

    The report contract must hold even when the session died — downstream
    tooling (status derivation, the task UI tab, chat replies) relies on the
    files existing.
    """
    now = datetime.now(timezone.utc).isoformat()
    report = f"""# UI Check Report

## Verdict
BLOCKED

## Environment
- URL: (see task parameters)
- Role/account: (see task parameters)
- Attempts: 0 / 1

## Steps performed
None — the check session did not complete.

## Expected vs actual
- Expected: the check to run in a browser
- Actual: {reason}

## Screenshots
None

## Console errors
None collected

## Network failures
None collected

## Issues found
None

## Limitations
The check could not be performed: {reason}
"""
    try:
        (spec_dir / "ui_check_report.md").write_text(report, encoding="utf-8")
        (spec_dir / "ui_check_result.json").write_text(
            json.dumps(
                {
                    "verdict": "BLOCKED",
                    "url": None,
                    "role": None,
                    "attempts_requested": 1,
                    "attempts_performed": 0,
                    "issues_count": 0,
                    "evidence_count": 0,
                    "blocked_reason": reason,
                    "written_by": "runner_fallback",
                    "timestamp": now,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        debug_error("ui_check", f"Failed to write BLOCKED fallback: {e}")


async def run_ui_check_session(
    project_dir: Path,
    spec_dir: Path,
    model: str | None,
    verbose: bool = False,
) -> str:
    """Run the single ui_checker agent session. Returns the final verdict."""
    task_logger = get_task_logger(spec_dir)
    current_tool = None

    prompt = get_ui_check_prompt(spec_dir, project_dir)
    debug("ui_check", "Loaded UI check prompt", prompt_length=len(prompt))

    ui_model = get_phase_model(spec_dir, "qa", model)
    thinking_budget = get_phase_thinking_budget(spec_dir, "qa")

    try:
        client = create_client(
            project_dir,
            spec_dir,
            ui_model,
            agent_type="ui_checker",
            max_thinking_tokens=thinking_budget,
        )
        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                msg_type = type(msg).__name__
                if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                    for block in msg.content:
                        block_type = type(block).__name__
                        if block_type == "TextBlock" and hasattr(block, "text"):
                            print(block.text, end="", flush=True)
                            if task_logger and block.text.strip():
                                task_logger.log(
                                    block.text,
                                    LogEntryType.TEXT,
                                    LogPhase.VALIDATION,
                                    print_to_console=False,
                                )
                        elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                            tool_name = block.name
                            inp = get_safe_tool_input(block)
                            tool_input_display = None
                            if inp:
                                if "url" in inp:
                                    tool_input_display = str(inp["url"])[:80]
                                elif "filename" in inp:
                                    tool_input_display = str(inp["filename"])[-60:]
                                elif "file_path" in inp:
                                    tool_input_display = str(inp["file_path"])[-60:]
                            if task_logger:
                                task_logger.tool_start(
                                    tool_name,
                                    tool_input_display,
                                    LogPhase.VALIDATION,
                                    print_to_console=True,
                                )
                            else:
                                print(f"\n[UI Check Tool: {tool_name}]", flush=True)
                            current_tool = tool_name
                elif msg_type == "UserMessage" and hasattr(msg, "content"):
                    for block in msg.content:
                        if type(block).__name__ == "ToolResultBlock":
                            is_error = getattr(block, "is_error", False)
                            if task_logger and current_tool:
                                task_logger.tool_end(
                                    current_tool,
                                    success=not is_error,
                                    result=(
                                        str(getattr(block, "content", ""))[:100]
                                        if is_error
                                        else None
                                    ),
                                    phase=LogPhase.VALIDATION,
                                )
                            elif is_error:
                                print(
                                    f"   [Error] {str(getattr(block, 'content', ''))[:200]}",
                                    flush=True,
                                )
                            else:
                                print("   [Done]", flush=True)
                            current_tool = None
    except Exception as e:
        debug_error("ui_check", f"UI check session exception: {e}")
        print(f"\nError during UI check session: {e}")
        if task_logger:
            task_logger.log_error(f"UI check session error: {e}", LogPhase.VALIDATION)
        if read_ui_check_verdict(spec_dir) is None:
            write_blocked_fallback(spec_dir, f"agent session error: {e}")
        return read_ui_check_verdict(spec_dir) or "BLOCKED"

    print("\n" + "-" * 70 + "\n")

    verdict = read_ui_check_verdict(spec_dir)
    if verdict is None:
        debug_error("ui_check", "Agent did not write ui_check_result.json")
        write_blocked_fallback(
            spec_dir,
            "the agent session finished without writing ui_check_result.json",
        )
        verdict = "BLOCKED"
    return verdict


def handle_ui_check_command(
    project_dir: Path,
    spec_dir: Path,
    model: str | None,
    verbose: bool = False,
) -> None:
    """Handle the --ui-check command (standalone browser verification).

    Args:
        project_dir: Project root directory
        spec_dir: Spec directory path
        model: Model override (falls back to task_metadata / defaults)
        verbose: Enable verbose output
    """
    print_banner()
    print(f"\nRunning UI check for: {spec_dir.name}\n")
    if not validate_environment(spec_dir):
        sys.exit(1)

    # A UI check is browser-based by definition: force the Playwright MCP for
    # the ui_checker agent regardless of the project's PLAYWRIGHT_MCP_ENABLED
    # flag (same override mechanism agent_service uses for bug tasks).
    import os

    os.environ.setdefault("AGENT_MCP_ui_checker_ADD", "playwright")

    debug_section("ui_check", f"UI Check: {spec_dir.name}")
    emit_phase(ExecutionPhase.QA_REVIEW, "Running browser UI check", progress=5)

    try:
        verdict = asyncio.run(
            run_ui_check_session(project_dir, spec_dir, model, verbose)
        )
    except KeyboardInterrupt:
        print("\n\nUI check interrupted.")
        if read_ui_check_verdict(spec_dir) is None:
            write_blocked_fallback(spec_dir, "the check was interrupted")
        emit_phase(ExecutionPhase.FAILED, "UI check interrupted")
        sys.exit(1)

    report_file = spec_dir / "ui_check_report.md"
    print(f"\n🔍 UI check verdict: {verdict}")
    print(f"   Report: {report_file}")

    debug_success("ui_check", f"UI check finished: {verdict}")
    # Any honest verdict (including FAIL/BLOCKED) is a *completed* check run —
    # the task is done, the verdict is the payload. FAILED is reserved for
    # infrastructure errors, handled above.
    emit_phase(ExecutionPhase.COMPLETE, f"UI check complete: {verdict}", progress=100)
