"""Project docs ingest: upload markdown / text files, index for retrieval.

POST /api/ext/projects/{project}/ingest-docs
    multipart/form-data: files[] = [...]
    Returns: { saved: int, indexed_sections: int, project: str, root: str }

Files land under PROJECTS_DATA_DIR/uploads/<project>/ on the server (which
powers Hermes chat via docs_index_service.reindex()), AND are mirrored into
<project>/.magestic-ai/uploaded-docs/ inside the actual project directory so
the coding agent can Read/Glob them like any other file in the workspace.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..config import get_settings
from ..services import docs_index_service
from .projects import load_projects

router = APIRouter()

_ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".org"}


def _uploads_root(project: str) -> Path:
    safe = "".join(c for c in project if c.isalnum() or c in "-_") or "default"
    root = Path(get_settings().PROJECTS_DATA_DIR) / "uploads" / safe
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slugify(name: str) -> str:
    """Match the slug logic used by AddProjectModal.tsx so projectSlug round-trips."""
    return (
        re.sub(r"[^a-z0-9-_]", "-", (name or "").lower())
        .strip("-")
        or ""
    )


def _project_mirror_dir(project_slug: str) -> Path | None:
    """Resolve the registered project that owns this slug and return its
    `.magestic-ai/uploaded-docs/` directory. Returns None if no match —
    upload still succeeds, just without the mirror.
    """
    for pdata in load_projects().values():
        name = pdata.get("name") or Path(pdata["path"]).name
        if _slugify(name) == project_slug or pdata.get("id") == project_slug:
            project_path = Path(pdata["path"])
            if not project_path.is_dir():
                return None
            mirror = project_path / ".magestic-ai" / "uploaded-docs"
            mirror.mkdir(parents=True, exist_ok=True)
            return mirror
    return None


class IngestResult(BaseModel):
    project: str
    saved: int
    rejected: list[str]
    indexed_sections: int
    files_indexed: int
    root: str


@router.post("/projects/{project}/ingest-docs", response_model=IngestResult)
async def ingest_docs(project: str, files: list[UploadFile] = File(...)) -> IngestResult:
    if not files:
        raise HTTPException(status_code=400, detail="no files in upload")
    root = _uploads_root(project)
    mirror = _project_mirror_dir(project)  # may be None if slug doesn't resolve
    saved = 0
    rejected: list[str] = []
    for f in files:
        name = Path(f.filename or "untitled").name
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            rejected.append(f"{name} (suffix {suffix!r} not in {sorted(_ALLOWED_SUFFIXES)})")
            continue
        contents = await f.read()
        if len(contents) > 5_000_000:
            rejected.append(f"{name} (>5MB)")
            continue
        (root / name).write_bytes(contents)
        if mirror is not None:
            # Best-effort: mirror failures don't fail the upload.
            try:
                (mirror / name).write_bytes(contents)
            except OSError:
                pass
        saved += 1
    if saved == 0:
        raise HTTPException(status_code=400, detail={"saved": 0, "rejected": rejected})

    stats = docs_index_service.reindex(project, root)
    return IngestResult(
        project=project,
        saved=saved,
        rejected=rejected,
        indexed_sections=stats["sections_indexed"],
        files_indexed=stats["files_indexed"],
        root=str(root),
    )
