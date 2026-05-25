"""Shared project-slug resolver.

Several routes (transcripts ingest, hermes grounding, paste-style transcript
storage) need to translate a project slug back to its on-disk path. The
slugification has to match `AddProjectModal.tsx` so the round-trip stays
stable. Centralising it here keeps the three callers in lockstep.
"""

from __future__ import annotations

import re
from pathlib import Path


def slugify(name: str) -> str:
    """Lowercase + collapse non-[a-z0-9-_] to '-'. Mirrors AddProjectModal.tsx."""
    return re.sub(r"[^a-z0-9-_]", "-", (name or "").lower()).strip("-") or ""


def resolve_project_dir(project_slug: str) -> Path | None:
    """Find the registered project that owns this slug; return its path or None.

    Looked up by either slugified name OR raw project id, so the frontend can
    pass whichever it has handy.
    """
    info = resolve_project_info(project_slug)
    return info[1] if info else None


def resolve_project_info(project_slug: str) -> tuple[str, Path] | None:
    """Like `resolve_project_dir`, but also returns the project id.

    Useful for callers that need to broadcast project-scoped WebSocket events
    (where the frontend matches on `projectId`, not the slug).
    """
    # Local import to avoid a circular import: routes.projects imports services
    # for some endpoints, and services pulling routes at module load time would
    # bind too early.
    from ..routes.projects import load_projects

    for pid, pdata in load_projects().items():
        name = pdata.get("name") or Path(pdata["path"]).name
        if slugify(name) == project_slug or pid == project_slug:
            project_path = Path(pdata["path"])
            if project_path.is_dir():
                return (pid, project_path)
    return None
