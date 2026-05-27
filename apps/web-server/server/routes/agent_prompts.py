"""
Agent Prompt routes — per-project custom agent prompts.

The bundled prompts in ``apps/backend/prompts/`` are the defaults. A project may
override any of them; overrides are stored sparsely in the ``agent_prompts``
table and materialized to disk before each agent run.

Endpoints:
- GET    /api/prompts/catalog                          — all overridable prompts
- GET    /api/projects/{project_id}/prompts            — catalog + override status
- GET    /api/projects/{project_id}/prompts/{key:path} — effective + default + override
- PUT    /api/projects/{project_id}/prompts/{key:path} — save an override
- DELETE /api/projects/{project_id}/prompts/{key:path} — reset to default
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import User
from ..database.engine import get_db
from ..services import agent_prompt_service as svc
from .auth_routes import get_current_user

logger = logging.getLogger(__name__)

# Catalog endpoint (not project-scoped).
catalog_router = APIRouter(prefix="/api/prompts", tags=["Agent Prompts"])

# Project-scoped CRUD. Mounted under /api/projects in main.py.
router = APIRouter(prefix="/api/projects", tags=["Agent Prompts"])


class PromptUpdate(BaseModel):
    content: str = Field(min_length=1)


def _require_valid_key(key: str) -> None:
    """Reject unknown keys — this also blocks path traversal (../, abs paths)."""
    if not svc.is_valid_key(key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown prompt key: {key}",
        )


def _require_known_project(project_id: str) -> None:
    """404 for unknown projects so overrides can't be keyed to arbitrary IDs.

    Mirrors update_project_settings in projects.py. Authentication itself is
    enforced at the router level (``require_active`` in main.py); this codebase
    uses a shared-workspace model with no per-project ACLs, so any active user
    may manage any registered project — we only require that it exists.
    """
    if svc.get_project_path(project_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )


@catalog_router.get("/catalog")
async def get_catalog() -> list[dict]:
    """Return metadata for every overridable bundled prompt."""
    return svc.list_catalog()


@router.get("/{project_id}/prompts")
async def list_project_prompts(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Return the catalog annotated with this project's override status."""
    _require_known_project(project_id)
    overrides = await svc.get_override_map(project_id, db)
    items: list[dict] = []
    for entry in svc.list_catalog():
        key = entry["key"]
        row = overrides.get(key)
        items.append(
            {
                **entry,
                "isOverridden": row is not None,
                "updatedAt": row.updated_at.isoformat() if row is not None else None,
            }
        )
    return items


@router.get("/{project_id}/prompts/{prompt_key:path}")
async def get_project_prompt(
    project_id: str,
    prompt_key: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the effective prompt: default, override (if any), and content."""
    _require_known_project(project_id)
    _require_valid_key(prompt_key)
    return await svc.get_effective(project_id, prompt_key, db)


@router.put("/{project_id}/prompts/{prompt_key:path}")
async def save_project_prompt(
    project_id: str,
    prompt_key: str,
    payload: PromptUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Create or update a project's override for a prompt."""
    _require_known_project(project_id)
    _require_valid_key(prompt_key)
    await svc.upsert_override(project_id, prompt_key, payload.content, user.id, db)
    return await svc.get_effective(project_id, prompt_key, db)


@router.delete("/{project_id}/prompts/{prompt_key:path}")
async def reset_project_prompt(
    project_id: str,
    prompt_key: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reset a prompt to its bundled default (delete the override row)."""
    _require_known_project(project_id)
    _require_valid_key(prompt_key)
    await svc.delete_override(project_id, prompt_key, db)
    return await svc.get_effective(project_id, prompt_key, db)
