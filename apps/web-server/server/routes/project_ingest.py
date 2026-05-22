"""Project docs ingest: upload markdown / text files, index for retrieval.

POST /api/ext/projects/{project}/ingest-docs
    multipart/form-data: files[] = [...]
    Returns: { saved: int, indexed_sections: int, project: str, root: str }

Files land under PROJECTS_DATA_DIR/uploads/<project>/ on the server and are
then run through docs_index_service.reindex() so /api/ext/docs-index/.../search
sees them immediately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..config import get_settings
from ..services import docs_index_service

router = APIRouter()

_ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".org"}


def _uploads_root(project: str) -> Path:
    safe = "".join(c for c in project if c.isalnum() or c in "-_") or "default"
    root = Path(get_settings().PROJECTS_DATA_DIR) / "uploads" / safe
    root.mkdir(parents=True, exist_ok=True)
    return root


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
