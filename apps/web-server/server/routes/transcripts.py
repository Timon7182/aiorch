"""Transcript ingest: upload audio / video / already-transcribed text into
a project so graphify can fold it into the knowledge graph.

POST /api/ext/projects/{project}/ingest-transcripts
    multipart/form-data: files[] = [...]
    Saves to <project>/.magestic-ai/transcripts/ inside the project tree.
    Audio/video gets transcribed by graphify's bundled faster-whisper on the
    next graph refresh; text files are read directly.
    Returns immediately with a 202 — the graphify refresh runs async.

Why a separate route from /ingest-docs:
    - Different file types (media + transcripts vs. markdown / text docs).
    - Different size profile (media files dwarf the 5MB doc limit).
    - Different destination (.magestic-ai/transcripts/ vs.
      .magestic-ai/uploaded-docs/) so users can curate them separately in
      the UI and so graphify's source attribution stays clean.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from ..services.project_resolve import resolve_project_dir

logger = logging.getLogger(__name__)
router = APIRouter()

# Audio + video formats faster-whisper handles natively, plus pre-
# transcribed text. Anything else gets rejected with a clear message.
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".vtt", ".srt"}
_ALLOWED_SUFFIXES = _AUDIO_SUFFIXES | _VIDEO_SUFFIXES | _TEXT_SUFFIXES

# Generous — meeting recordings are routinely 100MB+. 500MB matches the
# upper bound of what we want sitting in a project's working tree.
_MAX_BYTES = 500 * 1024 * 1024


class TranscriptIngestResult(BaseModel):
    project: str
    saved: int
    rejected: list[str]
    transcripts_dir: str
    graph_refresh: str  # "queued" | "skipped"


@router.post(
    "/projects/{project}/ingest-transcripts",
    response_model=TranscriptIngestResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_transcripts(
    project: str,
    files: list[UploadFile] = File(...),
) -> TranscriptIngestResult:
    if not files:
        raise HTTPException(status_code=400, detail="no files in upload")

    project_path = resolve_project_dir(project)
    if project_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"project {project!r} not registered or path missing",
        )

    transcripts_dir = project_path / ".magestic-ai" / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    rejected: list[str] = []
    for f in files:
        name = Path(f.filename or "untitled").name
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            rejected.append(
                f"{name} (suffix {suffix!r} not in audio/video/text)"
            )
            continue
        contents = await f.read()
        if len(contents) > _MAX_BYTES:
            rejected.append(f"{name} (>{_MAX_BYTES // (1024 * 1024)}MB)")
            continue
        try:
            (transcripts_dir / name).write_bytes(contents)
        except OSError as e:
            rejected.append(f"{name} (write failed: {e})")
            continue
        saved += 1

    if saved == 0:
        raise HTTPException(
            status_code=400,
            detail={"saved": 0, "rejected": rejected},
        )

    # We do NOT auto-trigger graphify here. graphify v0.8.16 doesn't yet
    # support the Claude Max subscription (no `claude-cli` backend in the
    # shipped CLI), so any auto-trigger would either burn Gemini tokens
    # silently or fail. The user runs `graphify extract` manually when
    # they want to fold new transcripts into the graph; Hermes and the
    # coder/planner agents read whatever graph happens to exist.
    return TranscriptIngestResult(
        project=project,
        saved=saved,
        rejected=rejected,
        transcripts_dir=str(transcripts_dir),
        graph_refresh="manual",
    )


@router.get("/projects/{project}/transcripts")
async def list_transcripts(project: str) -> dict[str, Any]:
    """List files currently sitting in <project>/.magestic-ai/transcripts/."""
    project_path = resolve_project_dir(project)
    if project_path is None:
        raise HTTPException(status_code=404, detail=f"project {project!r} not registered")
    transcripts_dir = project_path / ".magestic-ai" / "transcripts"
    if not transcripts_dir.is_dir():
        return {"success": True, "transcripts": []}
    out: list[dict[str, Any]] = []
    for p in sorted(transcripts_dir.iterdir()):
        if p.is_file():
            stat = p.stat()
            out.append({
                "name": p.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    return {"success": True, "transcripts": out}
