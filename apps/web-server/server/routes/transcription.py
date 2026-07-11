"""Audio transcription via the external Transcriber service.

POST /api/ext/projects/{project}/transcribe           multipart audio -> {job_id, status}
GET  /api/ext/projects/{project}/transcribe/{job_id}   poll status (+ text when done)
GET  /api/ext/projects/{project}/transcribe            list this project's jobs

The Transcriber returns a job id immediately and processes on the GPU one job at
a time; the frontend polls the status endpoint. Finished transcripts are saved
into <project>/.magestic-ai/transcripts/ so they appear in the Transcripts UI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from ..services import transcription_service

logger = logging.getLogger(__name__)
router = APIRouter()

_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".webm"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
_ALLOWED = _AUDIO_SUFFIXES | _VIDEO_SUFFIXES
_MAX_BYTES = 500 * 1024 * 1024


@router.post("/projects/{project}/transcribe", status_code=status.HTTP_202_ACCEPTED)
async def transcribe(
    project: str,
    audio: UploadFile = File(...),
    language: str | None = Form(default=None),
) -> dict[str, Any]:
    name = Path(audio.filename or "recording.webm").name
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type {suffix!r}; expected audio or video",
        )
    content = await audio.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(content) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"file exceeds {_MAX_BYTES // (1024*1024)}MB")

    try:
        return await transcription_service.submit(
            project=project,
            filename=name,
            content=content,
            content_type=audio.content_type or "application/octet-stream",
            language=(language or None),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"project {project!r} not registered")
    except RuntimeError as e:
        logger.error("transcriber submit failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/projects/{project}/transcribe/{job_id}")
async def transcribe_status(project: str, job_id: str) -> dict[str, Any]:
    try:
        return await transcription_service.status(project=project, job_id=job_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="job not found")
    except RuntimeError as e:
        logger.error("transcriber status failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/projects/{project}/transcribe")
async def transcribe_list(project: str) -> dict[str, Any]:
    return {"jobs": transcription_service.list_jobs(project)}
