"""
Prompt Loading Utilities
========================

Functions for loading agent prompts from markdown files.
Supports dynamic prompt assembly based on project type for context optimization.
Supports Quick Mode for simplified prompts (~70% fewer tokens).
"""

import json
import os
import re
from pathlib import Path

from .project_context import (
    detect_project_capabilities,
    get_mcp_tools_for_project,
    load_project_index,
)
from .prompt_resolver import resolve_prompt_file

# Directory containing the bundled (default) prompt files.
# prompts/ is a sibling directory of prompts_pkg/, so go up one level first.
# NOTE: per-project overrides are resolved via resolve_prompt_file(); this
# constant remains the bundled-default root for any direct references.
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def get_planner_prompt(spec_dir: Path) -> str:
    """
    Load the planner agent prompt with spec path injected.
    The planner creates subtask-based implementation plans.

    Args:
        spec_dir: Directory containing the spec.md file

    Returns:
        The planner prompt content with spec path
    """
    # Quick Mode: Use simplified prompt (~70% fewer tokens)
    if os.environ.get("QUICK_MODE") == "true":
        quick_prompt_file = resolve_prompt_file("planner_quick.md")
        if quick_prompt_file.is_file():
            prompt_file = quick_prompt_file
        else:
            prompt_file = resolve_prompt_file("planner.md")
    else:
        prompt_file = resolve_prompt_file("planner.md")

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Planner prompt not found at {prompt_file}\n"
            "Make sure the magestic-ai/prompts/planner.md file exists."
        )

    prompt = prompt_file.read_text()

    # Inject spec directory information at the beginning
    spec_context = f"""## SPEC LOCATION

Your spec file is located at: `{spec_dir}/spec.md`

🚨 CRITICAL FILE CREATION INSTRUCTIONS 🚨

You MUST use the Write tool to create these files in the spec directory:
- `{spec_dir}/implementation_plan.json` - Subtask-based implementation plan (USE WRITE TOOL!)
- `{spec_dir}/build-progress.txt` - Progress notes (USE WRITE TOOL!)
- `{spec_dir}/init.sh` - Environment setup script (USE WRITE TOOL!)

DO NOT just describe what these files should contain. You MUST actually call the Write tool
with the file path and complete content to create them.

The project root is the parent of magestic-ai/. Implement code in the project root, not in the spec directory.

---

"""
    return spec_context + _get_attachments_context(spec_dir) + prompt


def get_coding_prompt(spec_dir: Path) -> str:
    """
    Load the coding agent prompt with spec path injected.

    Args:
        spec_dir: Directory containing the spec.md and implementation_plan.json

    Returns:
        The coding agent prompt content with spec path
    """
    # Quick Mode: Use simplified prompt (~70% fewer tokens)
    if os.environ.get("QUICK_MODE") == "true":
        quick_prompt_file = resolve_prompt_file("coder_quick.md")
        if quick_prompt_file.is_file():
            prompt_file = quick_prompt_file
        else:
            prompt_file = resolve_prompt_file("coder.md")
    else:
        prompt_file = resolve_prompt_file("coder.md")

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Coding prompt not found at {prompt_file}\n"
            "Make sure the magestic-ai/prompts/coder.md file exists."
        )

    prompt = prompt_file.read_text()

    spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- Progress notes: `{spec_dir}/build-progress.txt`
- Recovery context: `{spec_dir}/memory/attempt_history.json`

The project root is the parent of magestic-ai/. All code goes in the project root, not in the spec directory.

---

"""

    # Check for recovery context (stuck subtasks, retry hints)
    recovery_context = _get_recovery_context(spec_dir)
    if recovery_context:
        spec_context += recovery_context

    # Check for human input file
    human_input_file = spec_dir / "HUMAN_INPUT.md"
    if human_input_file.exists():
        human_input = human_input_file.read_text().strip()
        if human_input:
            spec_context += f"""## HUMAN INPUT (READ THIS FIRST!)

The human has left you instructions. READ AND FOLLOW THESE CAREFULLY:

{human_input}

After addressing this input, you may delete or clear the HUMAN_INPUT.md file.

---

"""

    return spec_context + _get_attachments_context(spec_dir) + prompt


def _get_recovery_context(spec_dir: Path) -> str:
    """
    Get recovery context if there are failed attempts or stuck subtasks.

    Args:
        spec_dir: Spec directory containing memory/

    Returns:
        Recovery context string or empty string
    """
    import json

    attempt_history_file = spec_dir / "memory" / "attempt_history.json"

    if not attempt_history_file.exists():
        return ""

    try:
        with open(attempt_history_file) as f:
            history = json.load(f)

        # Check for stuck subtasks
        stuck_subtasks = history.get("stuck_subtasks", [])
        if stuck_subtasks:
            context = """## ⚠️ RECOVERY ALERT - STUCK SUBTASKS DETECTED

Some subtasks have been attempted multiple times without success. These subtasks need:
- A COMPLETELY DIFFERENT approach
- Possibly simpler implementation
- Or escalation to human if infeasible

Stuck subtasks:
"""
            for stuck in stuck_subtasks:
                context += f"- {stuck['subtask_id']}: {stuck['reason']} ({stuck['attempt_count']} attempts)\n"

            context += "\nBefore working on any subtask, check memory/attempt_history.json for previous attempts!\n\n---\n\n"
            return context

        # Check for subtasks with multiple attempts
        subtasks_with_retries = []
        for subtask_id, subtask_data in history.get("subtasks", {}).items():
            attempts = subtask_data.get("attempts", [])
            if len(attempts) > 1 and subtask_data.get("status") != "completed":
                subtasks_with_retries.append((subtask_id, len(attempts)))

        if subtasks_with_retries:
            context = """## ⚠️ RECOVERY CONTEXT - RETRY AWARENESS

Some subtasks have been attempted before. When working on these:
1. READ memory/attempt_history.json for the specific subtask
2. See what approaches were tried
3. Use a DIFFERENT approach

Subtasks with previous attempts:
"""
            for subtask_id, attempt_count in subtasks_with_retries:
                context += f"- {subtask_id}: {attempt_count} attempts\n"

            context += "\n---\n\n"
            return context

        return ""

    except (OSError, json.JSONDecodeError):
        return ""


def get_followup_planner_prompt(spec_dir: Path) -> str:
    """
    Load the follow-up planner agent prompt with spec path and key files injected.
    The follow-up planner adds new subtasks to an existing completed implementation plan.

    Args:
        spec_dir: Directory containing the completed spec and implementation_plan.json

    Returns:
        The follow-up planner prompt content with paths injected
    """
    prompt_file = resolve_prompt_file("followup_planner.md")

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Follow-up planner prompt not found at {prompt_file}\n"
            "Make sure the magestic-ai/prompts/followup_planner.md file exists."
        )

    prompt = prompt_file.read_text()

    # Inject spec directory information at the beginning
    spec_context = f"""## SPEC LOCATION (FOLLOW-UP MODE)

You are adding follow-up work to a **completed** spec.

**Key files in this spec directory:**
- Spec: `{spec_dir}/spec.md`
- Follow-up request: `{spec_dir}/FOLLOWUP_REQUEST.md` (READ THIS FIRST!)
- Implementation plan: `{spec_dir}/implementation_plan.json` (APPEND to this, don't replace)
- Progress notes: `{spec_dir}/build-progress.txt`
- Context: `{spec_dir}/context.json`
- Memory: `{spec_dir}/memory/`

**Important paths:**
- Spec directory: `{spec_dir}`
- Project root: Parent of magestic-ai/ (where code should be implemented)

**Your task:**
1. Read `{spec_dir}/FOLLOWUP_REQUEST.md` to understand what to add
2. Read `{spec_dir}/implementation_plan.json` to see existing phases/subtasks
3. ADD new phase(s) with pending subtasks to the existing plan
4. PRESERVE all existing subtasks and their statuses

---

"""
    return spec_context + _get_attachments_context(spec_dir) + prompt


def is_first_run(spec_dir: Path) -> bool:
    """
    Check if this is the first run (no valid implementation plan with subtasks exists yet).

    The spec runner may create a skeleton implementation_plan.json with empty phases.
    This function checks for actual phases with subtasks, not just file existence.

    Args:
        spec_dir: Directory containing spec files

    Returns:
        True if implementation_plan.json doesn't exist or has no subtasks
    """
    plan_file = spec_dir / "implementation_plan.json"

    if not plan_file.exists():
        return True

    try:
        with open(plan_file) as f:
            plan = json.load(f)

        # Check if there are any phases with subtasks
        phases = plan.get("phases", [])
        if not phases:
            return True

        # Check if any phase has subtasks
        total_subtasks = sum(len(phase.get("subtasks", [])) for phase in phases)
        return total_subtasks == 0
    except (OSError, json.JSONDecodeError):
        # If we can't read the file, treat as first run
        return True


def _get_attachments_context(spec_dir: Path) -> str:
    """Return a prompt section listing client-attached screenshots, if any.

    The web-server materializes pasted screenshots into ``<spec_dir>/attachments/``
    (see routes/tasks.py ``_materialize_attachments``). Agents' Read tool renders
    images natively, so we point them at the files. Returns "" when no
    attachments exist (backward compatible).
    """
    attachments_dir = spec_dir / "attachments"
    if not attachments_dir.is_dir():
        return ""
    try:
        files = sorted(
            p
            for p in attachments_dir.iterdir()
            if p.is_file()
            and p.suffix.lower()
            in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
        )
    except OSError:
        return ""
    if not files:
        return ""
    listing = "\n".join(f"- `{p}`" for p in files)
    return (
        "## CLIENT-ATTACHED SCREENSHOTS\n\n"
        f"The client attached {len(files)} screenshot(s) with this task. "
        "Use the Read tool to view them (images render natively in your context):\n\n"
        f"{listing}\n\n---\n\n"
    )


def _get_bug_repro_context(spec_dir: Path, role: str) -> str:
    """Return the bug-reproduction protocol section for QA prompts on bug tasks.

    Gated on ``task_metadata.json`` ``taskType == 'bug'``. ``role`` is
    ``'reviewer'`` or ``'fixer'``. Injects the client's structured bug report
    (steps/expected/actual from requirements.json metadata) followed by the
    ``qa_bug_repro.md`` protocol. Returns "" for non-bug tasks (backward
    compatible).
    """
    task_type = ""
    try:
        tm_file = spec_dir / "task_metadata.json"
        if tm_file.is_file():
            task_type = (json.loads(tm_file.read_text()) or {}).get("taskType", "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    if task_type != "bug":
        return ""

    # Client's structured bug report (from requirements.json metadata)
    bug = {}
    try:
        req_file = spec_dir / "requirements.json"
        if req_file.is_file():
            meta = (json.loads(req_file.read_text()) or {}).get("metadata") or {}
            bug = meta.get("bugReport") or {}
    except (OSError, json.JSONDecodeError, TypeError):
        bug = {}

    client_report = "## CLIENT BUG REPORT\n\n"
    if isinstance(bug, dict) and bug.get("steps"):
        client_report += f"**Steps to reproduce:**\n{bug['steps']}\n\n"
    if isinstance(bug, dict) and bug.get("expected"):
        client_report += f"**Expected behavior:**\n{bug['expected']}\n\n"
    if isinstance(bug, dict) and bug.get("actual"):
        client_report += f"**Actual behavior:**\n{bug['actual']}\n\n"
    if not bug:
        client_report += (
            "(No structured steps provided — derive the reproduction steps from "
            "the task description and any attached screenshots.)\n\n"
        )
    client_report += "---\n\n"

    try:
        protocol = _load_prompt_file("qa_bug_repro.md")
    except FileNotFoundError:
        protocol = ""

    role_note = ""
    if role == "fixer":
        role_note = (
            "\n\n## FIXER: RE-VERIFY IN BROWSER AFTER FIX\n\n"
            "After applying your fix, re-run the client's reproduction steps in the "
            "browser, capture AFTER screenshots into `evidence/`, and append a "
            '"Verification after fix" section to `reproduction_report.md` confirming '
            "the bug no longer reproduces.\n"
        )

    return client_report + protocol + role_note + "\n\n---\n\n"


def _is_safe_target_url(url: str | None) -> bool:
    """Only http(s) URLs with a host are acceptable UI-check browser targets.

    Mirrors server.services.ui_check_service.is_valid_target_url (web-server);
    duplicated here because the backend must also gate direct CLI runs where
    agent_service's resolution never happened.
    """
    if not url or not isinstance(url, str):
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def get_ui_check_prompt(spec_dir: Path, project_dir: Path) -> str:
    """Build the full prompt for a standalone UI-check session.

    Used by ``run.py --spec X --ui-check`` (agent_type ``ui_checker``). Assembles:
    1. Spec context (paths the agent may write to)
    2. CHECK PARAMETERS from ``task_metadata.json`` ``uiCheck`` +
       ``requirements.json`` (description as functionality fallback)
    3. Credential placeholder names from the ``UI_CHECK_SECRET_VARS`` env var
       (names only — values are substituted by the MCP secret proxy and never
       enter the session)
    4. The ``ui_check.md`` protocol

    Args:
        spec_dir: Directory containing the spec files
        project_dir: Root directory of the project

    Returns:
        The assembled UI-check prompt
    """
    protocol = _load_prompt_file("ui_check.md")

    ui_check = {}
    try:
        tm_file = spec_dir / "task_metadata.json"
        if tm_file.is_file():
            ui_check = (json.loads(tm_file.read_text()) or {}).get("uiCheck") or {}
    except (OSError, json.JSONDecodeError, TypeError):
        ui_check = {}

    title, description = "", ""
    try:
        req_file = spec_dir / "requirements.json"
        if req_file.is_file():
            req = json.loads(req_file.read_text()) or {}
            title = req.get("title") or ""
            description = req.get("description") or ""
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    # URL: agent_service resolves the final target (metadata → named env →
    # preview) and exports UI_CHECK_TARGET_URL; metadata is the fallback for
    # direct CLI runs. Only http(s) targets are acceptable browser targets —
    # a file:// / data: / javascript: value from metadata must never reach the
    # prompt (the liveness probe curls it and Playwright would navigate to it).
    raw_url = os.environ.get("UI_CHECK_TARGET_URL") or ui_check.get("url") or ""
    target_url = raw_url if _is_safe_target_url(raw_url) else ""

    params = "## CHECK PARAMETERS\n\n"
    params += f"- **Target URL:** {target_url or '(NOT PROVIDED — see rule 6: BLOCKED)'}\n"
    if ui_check.get("environment"):
        params += f"- **Environment:** {ui_check['environment']}\n"
    params += f"- **Role/account:** {ui_check.get('role') or 'none specified'}\n"
    attempts = ui_check.get("attempts") or 1
    try:
        attempts = max(1, min(3, int(attempts)))
    except (TypeError, ValueError):
        attempts = 1
    params += f"- **Attempts requested:** {attempts}\n\n"
    if title or description:
        params += f"**Functionality under check:** {title}\n\n{description}\n\n"
    if ui_check.get("preconditions"):
        params += f"**Preconditions:**\n{ui_check['preconditions']}\n\n"
    if ui_check.get("steps"):
        params += f"**Steps to perform:**\n{ui_check['steps']}\n\n"
    else:
        params += (
            "**Steps to perform:** none provided — derive minimal steps from the "
            "functionality description and mark them as derived in the report.\n\n"
        )
    if ui_check.get("expected"):
        params += f"**Expected result:**\n{ui_check['expected']}\n\n"

    secret_vars = [
        v.strip()
        for v in os.environ.get("UI_CHECK_SECRET_VARS", "").split(",")
        if v.strip()
    ]
    if secret_vars:
        placeholders = "\n".join(f"- `${{{v}}}`" for v in secret_vars)
        params += (
            "**Credential placeholders** (type these EXACT literal strings into "
            "login fields; real values are substituted outside your session):\n"
            f"{placeholders}\n\n"
        )
    else:
        params += (
            "**Credentials:** none configured. If the app requires login, the "
            "verdict is BLOCKED (state that credentials are required).\n\n"
        )

    spec_context = f"""## SPEC LOCATION

- Spec directory (the ONLY place you write files): `{spec_dir}`
- Report output: `{spec_dir}/ui_check_report.md`
- Result JSON: `{spec_dir}/ui_check_result.json`
- Evidence directory: `{spec_dir}/evidence-ui-check/`
- Project root (read-only for you): `{project_dir}`

---

"""

    return (
        spec_context
        + params
        + "---\n\n"
        + _get_attachments_context(spec_dir)
        + protocol
    )


def _load_prompt_file(filename: str) -> str:
    """
    Load a prompt file from the prompts directory.

    Args:
        filename: Relative path to prompt file (e.g., "qa_reviewer.md" or "mcp_tools/playwright_browser.md")

    Returns:
        Content of the prompt file

    Raises:
        FileNotFoundError: If prompt file doesn't exist
    """
    prompt_file = resolve_prompt_file(filename)
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    return prompt_file.read_text()


def get_qa_reviewer_prompt(spec_dir: Path, project_dir: Path) -> str:
    """
    Load the QA reviewer prompt with project-specific MCP tools dynamically injected.

    This function:
    1. Loads the base QA reviewer prompt
    2. Detects project capabilities from project_index.json
    3. Injects only relevant MCP tool documentation (Electron, Puppeteer, DB, API)

    This saves context window by excluding irrelevant tool docs.
    For example, a CLI Python project won't get Electron validation docs.

    Args:
        spec_dir: Directory containing the spec files
        project_dir: Root directory of the project

    Returns:
        The QA reviewer prompt with project-specific tools injected
    """
    # Quick Mode: Use simplified prompt (~70% fewer tokens)
    # Note: Quick mode uses simplified prompt without MCP tool injection
    if os.environ.get("QUICK_MODE") == "true":
        quick_prompt_file = resolve_prompt_file("qa_reviewer_quick.md")
        if quick_prompt_file.is_file():
            base_prompt = quick_prompt_file.read_text()
            # Add basic spec context and return (skip MCP tool injection for speed)
            spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- QA report output: `{spec_dir}/qa_report.md`

The project root is: `{project_dir}`

---

"""
            return (
                spec_context
                + _get_attachments_context(spec_dir)
                + _get_bug_repro_context(spec_dir, "reviewer")
                + base_prompt
            )

    # Load base QA reviewer prompt (full mode with MCP tools)
    base_prompt = _load_prompt_file("qa_reviewer.md")

    # Load project index and detect capabilities
    project_index = load_project_index(project_dir)
    capabilities = detect_project_capabilities(project_index)

    # Get list of MCP tool doc files to include
    mcp_tool_files = get_mcp_tools_for_project(capabilities)

    # Load and assemble MCP tool sections
    mcp_sections = []
    for tool_file in mcp_tool_files:
        try:
            section = _load_prompt_file(tool_file)
            mcp_sections.append(section)
        except FileNotFoundError:
            # Skip missing files gracefully
            pass

    # Inject spec context at the beginning
    spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- Progress notes: `{spec_dir}/build-progress.txt`
- QA report output: `{spec_dir}/qa_report.md`
- Fix request output: `{spec_dir}/QA_FIX_REQUEST.md`

The project root is: `{project_dir}`

---

## PROJECT CAPABILITIES DETECTED

"""

    # Add capability summary for transparency
    active_caps = [k for k, v in capabilities.items() if v]
    if active_caps:
        spec_context += (
            "Based on project analysis, the following capabilities were detected:\n"
        )
        for cap in active_caps:
            cap_name = (
                cap.replace("is_", "").replace("has_", "").replace("_", " ").title()
            )
            spec_context += f"- {cap_name}\n"
        spec_context += "\nRelevant validation tools have been included below.\n\n"
    else:
        spec_context += (
            "No special project capabilities detected. Using standard validation.\n\n"
        )

    spec_context += "---\n\n"

    # Find injection point in base prompt (after PHASE 4, before PHASE 5)
    injection_marker = (
        "<!-- PROJECT-SPECIFIC VALIDATION TOOLS WILL BE INJECTED HERE -->"
    )

    if mcp_sections and injection_marker in base_prompt:
        # Replace marker with actual MCP tool sections
        mcp_content = "\n\n---\n\n## PROJECT-SPECIFIC VALIDATION TOOLS\n\n"
        mcp_content += "The following validation tools are available based on your project type:\n\n"
        mcp_content += "\n\n---\n\n".join(mcp_sections)
        mcp_content += "\n\n---\n"

        # Replace the multi-line marker comment block
        marker_pattern = r"<!-- PROJECT-SPECIFIC VALIDATION TOOLS WILL BE INJECTED HERE -->.*?<!-- - API validation \(for projects with API endpoints\) -->"
        base_prompt = re.sub(marker_pattern, mcp_content, base_prompt, flags=re.DOTALL)
    elif mcp_sections:
        # Fallback: append at the end if marker not found
        base_prompt += "\n\n---\n\n## PROJECT-SPECIFIC VALIDATION TOOLS\n\n"
        base_prompt += "\n\n---\n\n".join(mcp_sections)

    return (
        spec_context
        + _get_attachments_context(spec_dir)
        + _get_bug_repro_context(spec_dir, "reviewer")
        + base_prompt
    )


def get_qa_fixer_prompt(spec_dir: Path, project_dir: Path) -> str:
    """
    Load the QA fixer prompt with spec paths injected.

    Args:
        spec_dir: Directory containing the spec files
        project_dir: Root directory of the project

    Returns:
        The QA fixer prompt content with paths injected
    """
    base_prompt = _load_prompt_file("qa_fixer.md")

    spec_context = f"""## SPEC LOCATION

Your spec and progress files are located at:
- Spec: `{spec_dir}/spec.md`
- Implementation plan: `{spec_dir}/implementation_plan.json`
- QA fix request: `{spec_dir}/QA_FIX_REQUEST.md` (READ THIS FIRST!)
- QA report: `{spec_dir}/qa_report.md`

The project root is: `{project_dir}`

---

"""
    return (
        spec_context
        + _get_attachments_context(spec_dir)
        + _get_bug_repro_context(spec_dir, "fixer")
        + base_prompt
    )
