"""
Token usage routes.

Reads per-spec `usage.json` files (written by `core/usage_event.py` on the
backend) and exposes them via REST. Two scopes:

  GET /api/usage/tasks/{task_id}        full breakdown for a single task
  GET /api/usage/projects/{project_id}  rollup across every task in a project
  GET /api/usage/global                 rollup across every project

Frontend uses these to render per-task token pills and the dashboard total.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .projects import load_projects
from .tasks import _resolve_task

router = APIRouter()
logger = logging.getLogger(__name__)

USAGE_FILENAME = "usage.json"


def _empty_totals() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
    }


def _read_usage_file(usage_file: Path) -> dict[str, Any] | None:
    """Parse a single usage.json file, normalising its shape. None if absent."""
    if not usage_file.exists():
        return None
    try:
        data = json.loads(usage_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[usage] failed to read {usage_file}: {exc}")
        return None
    # Defensive: ensure expected shape.
    data.setdefault("totals", _empty_totals())
    data.setdefault("by_phase", {})
    data.setdefault("by_model", {})
    data.setdefault("events", [])
    return data


def _worktree_usage_file(spec_dir: Path) -> Path | None:
    """Resolve the worktree copy of usage.json for a main spec dir.

    The agent always writes usage.json into the *worktree* spec dir during a
    build. Web-initiated runs sync it back to the main spec dir, but CLI runs
    (and any sync that hasn't fired yet, or that failed) leave it only in the
    worktree — which is why per-task usage can read as zero. We derive the
    worktree path so reads work regardless of how the build was launched.

    Layout: ``<project>/.magestic-ai/specs/<id>`` (main) maps to
    ``<project>/.magestic-ai/worktrees/tasks/<id>/.magestic-ai/specs/<id>``.
    """
    try:
        spec_id = spec_dir.name
        project_root = spec_dir.parents[2]
    except IndexError:
        return None
    return (
        project_root
        / ".magestic-ai"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".magestic-ai"
        / "specs"
        / spec_id
        / USAGE_FILENAME
    )


def _load_usage(spec_dir: Path) -> dict[str, Any] | None:
    """Load a spec's usage.json, falling back to the worktree copy.

    Prefers the synced main-spec file; if it's missing or has recorded no
    calls yet, transparently reads the worktree copy so the dashboard and
    per-task tab still show real numbers. Returns None when neither exists.
    """
    data = _read_usage_file(spec_dir / USAGE_FILENAME)
    if data is not None and data["totals"].get("calls", 0) > 0:
        return data

    worktree_file = _worktree_usage_file(spec_dir)
    if worktree_file is not None:
        worktree_data = _read_usage_file(worktree_file)
        if worktree_data is not None and worktree_data["totals"].get("calls", 0) > 0:
            return worktree_data

    return data


def _accumulate(target: dict[str, Any], source: dict[str, Any]) -> None:
    for k in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "calls",
    ):
        target[k] = int(target.get(k, 0)) + int(source.get(k, 0) or 0)
    target["cost_usd"] = float(target.get("cost_usd", 0.0)) + float(
        source.get("cost_usd", 0.0) or 0.0
    )


def _summarize_task(project_id: str, spec_dir: Path) -> dict[str, Any]:
    """Build a compact per-task summary suitable for project-level rollups."""
    usage = _load_usage(spec_dir)
    totals = usage["totals"] if usage else _empty_totals()
    return {
        "taskId": f"{project_id}:{spec_dir.name}",
        "specId": spec_dir.name,
        "totals": totals,
        "hasData": usage is not None and totals.get("calls", 0) > 0,
        "updatedAt": (usage or {}).get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}")
async def get_task_usage(task_id: str) -> dict[str, Any]:
    """Return token usage for a single task.

    Returns an empty (zeroed) structure with `hasData=false` when no agent
    session has been recorded yet — so the frontend can render a placeholder
    instead of a 404.
    """
    project_id, spec_id, _project_path, spec_dir = _resolve_task(task_id)
    usage = _load_usage(spec_dir)
    if usage is None:
        return {
            "taskId": task_id,
            "projectId": project_id,
            "specId": spec_id,
            "hasData": False,
            "totals": _empty_totals(),
            "byPhase": {},
            "byModel": {},
            "events": [],
        }
    return {
        "taskId": task_id,
        "projectId": project_id,
        "specId": spec_id,
        "hasData": usage["totals"].get("calls", 0) > 0,
        "createdAt": usage.get("created_at"),
        "updatedAt": usage.get("updated_at"),
        "totals": usage["totals"],
        "byPhase": usage["by_phase"],
        "byModel": usage["by_model"],
        # Keep the last 200 events for the per-task chart; older points are
        # already folded into totals.
        "events": usage["events"][-200:],
    }


def _project_specs(project_id: str) -> tuple[Path, list[Path]]:
    """Return (project_path, list of spec dirs). Raises 404 if project unknown."""
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project_path = Path(projects[project_id]["path"])
    specs_dir = project_path / ".magestic-ai" / "specs"
    if not specs_dir.exists():
        return project_path, []
    return project_path, sorted(d for d in specs_dir.iterdir() if d.is_dir())


def _load_project_session_usage(project_path: Path) -> dict[str, dict[str, Any]]:
    """Load all project-level session usage files (Hermes, Insights chat).

    Returns `{feature: usage_dict}`. Each usage_dict has the same shape as
    a per-spec usage.json (totals/by_phase/by_model/events).
    """
    out: dict[str, dict[str, Any]] = {}
    usage_dir = project_path / ".magestic-ai" / "usage"
    if not usage_dir.exists():
        return out
    for file in usage_dir.iterdir():
        if not file.is_file() or file.suffix != ".json":
            continue
        feature = file.stem  # e.g. "hermes", "insights"
        try:
            data = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[usage] failed to read {file}: {exc}")
            continue
        data.setdefault("totals", _empty_totals())
        data.setdefault("by_phase", {})
        data.setdefault("by_model", {})
        data.setdefault("events", [])
        out[feature] = data
    return out


@router.get("/projects/{project_id}")
async def get_project_usage(project_id: str) -> dict[str, Any]:
    """Aggregate token usage across every task + session feature in a project."""
    project_path, spec_dirs = _project_specs(project_id)

    totals = _empty_totals()
    by_phase: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    # New "by feature" axis: spec-runs vs. hermes vs. insights chat. The
    # frontend uses this to break down what's burning tokens on each project.
    by_feature: dict[str, dict[str, Any]] = {}
    tasks_summary: list[dict[str, Any]] = []

    # --- Per-spec (agent task) usage ----------------------------------------
    spec_feature_totals = _empty_totals()
    for spec_dir in spec_dirs:
        usage = _load_usage(spec_dir)
        if usage is not None:
            _accumulate(totals, usage["totals"])
            _accumulate(spec_feature_totals, usage["totals"])
            for phase, phase_data in usage["by_phase"].items():
                bucket = by_phase.setdefault(phase, _empty_totals())
                _accumulate(bucket, phase_data)
            for model, model_data in usage["by_model"].items():
                bucket = by_model.setdefault(model, _empty_totals())
                _accumulate(bucket, model_data)
        tasks_summary.append(_summarize_task(project_id, spec_dir))
    if spec_feature_totals["calls"] > 0:
        by_feature["agent"] = spec_feature_totals

    # --- Session usage (Hermes, Insights chat) ------------------------------
    for feature, usage in _load_project_session_usage(project_path).items():
        _accumulate(totals, usage["totals"])
        feature_bucket = by_feature.setdefault(feature, _empty_totals())
        _accumulate(feature_bucket, usage["totals"])
        for phase, phase_data in usage["by_phase"].items():
            bucket = by_phase.setdefault(phase, _empty_totals())
            _accumulate(bucket, phase_data)
        for model, model_data in usage["by_model"].items():
            bucket = by_model.setdefault(model, _empty_totals())
            _accumulate(bucket, model_data)

    return {
        "projectId": project_id,
        "totals": totals,
        "byPhase": by_phase,
        "byModel": by_model,
        "byFeature": by_feature,
        "tasks": tasks_summary,
        "taskCount": len(tasks_summary),
        "tasksWithData": sum(1 for t in tasks_summary if t["hasData"]),
    }


@router.get("/global")
async def get_global_usage() -> dict[str, Any]:
    """Aggregate usage across every project. Powers the global sidebar view."""
    projects = load_projects()
    totals = _empty_totals()
    per_project: list[dict[str, Any]] = []

    for project_id, project_data in projects.items():
        project_path = Path(project_data["path"])
        specs_dir = project_path / ".magestic-ai" / "specs"
        proj_totals = _empty_totals()
        if specs_dir.exists():
            for spec_dir in specs_dir.iterdir():
                if not spec_dir.is_dir():
                    continue
                usage = _load_usage(spec_dir)
                if usage is not None:
                    _accumulate(proj_totals, usage["totals"])
                    _accumulate(totals, usage["totals"])
        # Fold in Hermes/Insights chat usage so global stats match the
        # per-project page.
        for _feature, usage in _load_project_session_usage(project_path).items():
            _accumulate(proj_totals, usage["totals"])
            _accumulate(totals, usage["totals"])
        per_project.append({
            "projectId": project_id,
            "name": project_data.get("name") or Path(project_data["path"]).name,
            "totals": proj_totals,
        })

    per_project.sort(key=lambda p: p["totals"]["cost_usd"], reverse=True)

    return {
        "totals": totals,
        "projects": per_project,
    }
