"""
Agent Prompt Service
====================

Per-project agent prompt overrides.

The SQLite ``agent_prompts`` table is the source of truth and is sparse: a row
exists only for a prompt a project has actually customized. Bundled prompts in
``apps/backend/prompts/`` provide the defaults for everything else.

Because the backend agents run as a subprocess that reads prompts from disk,
``materialize_overrides`` writes a project's override rows to
``<project>/.magestic-ai/prompts/<key>`` right before an agent run (and prunes
files for prompts that were reset). The backend resolver
(``prompts_pkg.prompt_resolver``) reads from that directory first when
``MAGESTIC_PROMPT_OVERRIDE_DIR`` points to it.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database.models import AgentPrompt

logger = logging.getLogger(__name__)

# Subdirectory under a project's .magestic-ai/ where overrides are materialized.
OVERRIDE_SUBDIR = (".magestic-ai", "prompts")  # .magestic-ai/prompts


# ---------------------------------------------------------------------------
# Bundled-prompt catalog
# ---------------------------------------------------------------------------


def _bundled_dir() -> Path:
    """Root of the bundled prompts shipped with the backend."""
    return Path(get_settings().BACKEND_PATH) / "prompts"


def _category_for(key: str) -> str:
    """Group a prompt key into a UI category."""
    if key.startswith("github/"):
        return "github"
    if key.startswith("mcp_tools/"):
        return "mcp_tools"
    if key.startswith("analysis/"):
        return "analysis"
    name = key.rsplit("/", 1)[-1]
    if name.startswith(("planner", "coder", "followup_planner")):
        return "build"
    if name.startswith("qa_"):
        return "qa"
    if name.startswith("spec_") or name in {
        "complexity_assessor.md",
        "architecture.md",
        "validation_fixer.md",
    }:
        return "spec"
    if name.startswith("ideation_"):
        return "ideation"
    if name.startswith("roadmap_") or name == "competitor_analysis.md":
        return "roadmap"
    if name in {"doc_generator.md"}:
        return "docs"
    return "other"


def _display_name(key: str) -> str:
    """Human-friendly title derived from the prompt key."""
    name = key.rsplit("/", 1)[-1].removesuffix(".md")
    return name.replace("_", " ").title()


@lru_cache(maxsize=1)
def _build_catalog() -> list[dict]:
    """Scan the bundled prompts directory and build the catalog.

    Cached because the bundled set is static for the life of the process.
    """
    bundled = _bundled_dir()
    entries: dict[str, dict] = {}

    if bundled.is_dir():
        for path in sorted(bundled.rglob("*.md")):
            key = path.relative_to(bundled).as_posix()
            entries[key] = {
                "key": key,
                "category": _category_for(key),
                "displayName": _display_name(key),
                "sizeBytes": path.stat().st_size,
            }

    # insight_extractor.md lives outside prompts/ (analysis/prompts/), exposed
    # under a synthetic key the backend resolver special-cases.
    insight = Path(get_settings().BACKEND_PATH) / "analysis" / "prompts" / "insight_extractor.md"
    if insight.is_file():
        key = "analysis/insight_extractor.md"
        entries[key] = {
            "key": key,
            "category": "analysis",
            "displayName": _display_name(key),
            "sizeBytes": insight.stat().st_size,
        }

    return list(entries.values())


def list_catalog() -> list[dict]:
    """Return metadata for every bundled (overridable) prompt."""
    return list(_build_catalog())


@lru_cache(maxsize=1)
def catalog_keys() -> frozenset[str]:
    """Set of valid prompt keys — used to validate input and prevent traversal."""
    return frozenset(e["key"] for e in _build_catalog())


def is_valid_key(key: str) -> bool:
    return key in catalog_keys()


def _bundled_path_for(key: str) -> Path:
    """Real on-disk bundled path for a key (handles the analysis special case)."""
    if key == "analysis/insight_extractor.md":
        return Path(get_settings().BACKEND_PATH) / "analysis" / "prompts" / "insight_extractor.md"
    return _bundled_dir() / key


def read_default(key: str) -> str:
    """Return the bundled default content for a prompt key (empty if missing)."""
    path = _bundled_path_for(key)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Override CRUD (DB)
# ---------------------------------------------------------------------------


async def get_override_map(project_id: str, db: AsyncSession) -> dict[str, AgentPrompt]:
    """Return {prompt_key: AgentPrompt} for a project's existing overrides."""
    result = await db.execute(
        select(AgentPrompt).where(AgentPrompt.project_id == project_id)
    )
    return {row.prompt_key: row for row in result.scalars().all()}


async def get_effective(project_id: str, key: str, db: AsyncSession) -> dict:
    """Return the effective prompt for a key: default + override + content."""
    default = read_default(key)
    result = await db.execute(
        select(AgentPrompt).where(
            AgentPrompt.project_id == project_id,
            AgentPrompt.prompt_key == key,
        )
    )
    row = result.scalar_one_or_none()
    override = row.content if row is not None else None
    return {
        "key": key,
        "category": _category_for(key),
        "displayName": _display_name(key),
        "default": default,
        "override": override,
        "isOverridden": override is not None,
        "content": override if override is not None else default,
        "updatedAt": row.updated_at.isoformat() if row is not None else None,
    }


async def upsert_override(
    project_id: str, key: str, content: str, user_id: str | None, db: AsyncSession
) -> AgentPrompt:
    """Create or update a project's override for a prompt key."""
    result = await db.execute(
        select(AgentPrompt).where(
            AgentPrompt.project_id == project_id,
            AgentPrompt.prompt_key == key,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = AgentPrompt(
            project_id=project_id,
            prompt_key=key,
            content=content,
            updated_by=user_id,
        )
        db.add(row)
    else:
        row.content = content
        row.updated_by = user_id
    await db.commit()
    await db.refresh(row)
    return row


async def delete_override(project_id: str, key: str, db: AsyncSession) -> bool:
    """Delete a project's override (reset to default). Returns True if removed."""
    result = await db.execute(
        delete(AgentPrompt).where(
            AgentPrompt.project_id == project_id,
            AgentPrompt.prompt_key == key,
        )
    )
    await db.commit()
    return (result.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# Materialization (DB -> disk, consumed by the backend subprocess)
# ---------------------------------------------------------------------------


def override_dir_for(project_path: Path) -> Path:
    """The directory the backend resolver reads overrides from for a project."""
    return project_path.joinpath(*OVERRIDE_SUBDIR)


async def materialize_overrides(
    project_id: str, project_path: Path, db: AsyncSession
) -> Path:
    """Write a project's override rows to disk and prune reset prompts.

    Returns the override directory (to be set as MAGESTIC_PROMPT_OVERRIDE_DIR).
    Only files whose relative path is a known catalog key are pruned, so
    unrelated files in the directory are never touched.
    """
    override_dir = override_dir_for(project_path)
    overrides = await get_override_map(project_id, db)
    valid_keys = catalog_keys()

    # Write current overrides (only for valid keys).
    written: set[str] = set()
    for key, row in overrides.items():
        if key not in valid_keys:
            continue
        target = override_dir / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(row.content, encoding="utf-8")
        written.add(key)

    # Prune stale override files (resets) without disturbing foreign files.
    if override_dir.is_dir():
        for path in override_dir.rglob("*.md"):
            try:
                key = path.relative_to(override_dir).as_posix()
            except ValueError:
                continue
            if key in valid_keys and key not in written:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Could not prune stale prompt %s: %s", path, exc)

    logger.info(
        "[AgentPrompts] Materialized %d override(s) for project %s -> %s",
        len(written),
        project_id,
        override_dir,
    )
    return override_dir


# ---------------------------------------------------------------------------
# Project path lookup (projects.json is the canonical registry)
# ---------------------------------------------------------------------------


def get_project_path(project_id: str) -> Path | None:
    """Resolve a project's filesystem path from projects.json."""
    projects_file = Path(get_settings().PROJECTS_DATA_DIR) / "projects.json"
    if not projects_file.is_file():
        return None
    try:
        projects = json.loads(projects_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    data = projects.get(project_id)
    if not data or not data.get("path"):
        return None
    return Path(data["path"])
