"""Bridge to the external Transcriber service (GPU faster-whisper).

Flow: upload audio -> submit to the Transcriber -> it returns a job id
immediately -> we poll it -> when complete we persist the transcript into the
project's transcripts dir (via transcripts_service) so it flows into the
existing Transcripts UI and graphify.

Config (env, matches repo convention of reading service keys from os.environ):
    TRANSCRIBER_URL    default http://192.168.88.39:8200
    TRANSCRIBER_TOKEN  ingest bearer token created in the Transcriber UI
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import transcripts_service
from .project_resolve import resolve_project_dir


def _base_url() -> str:
    return os.environ.get("TRANSCRIBER_URL", "http://192.168.88.39:8200").rstrip("/")


def _token() -> str:
    return os.environ.get("TRANSCRIBER_TOKEN", "")


def _auth_headers() -> dict[str, str]:
    tok = _token()
    if not tok:
        raise RuntimeError("TRANSCRIBER_TOKEN is not set")
    return {"Authorization": f"Bearer {tok}"}


def _jobs_dir(project: str) -> Path:
    project_path = resolve_project_dir(project)
    if project_path is None:
        raise LookupError(project)
    d = project_path / ".magestic-ai" / "transcripts" / ".jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_file(project: str, job_id: str) -> Path:
    return _jobs_dir(project) / f"{job_id}.json"


def _load_job(project: str, job_id: str) -> dict[str, Any] | None:
    p = _job_file(project, job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save_job(project: str, record: dict[str, Any]) -> None:
    _job_file(project, record["job_id"]).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def submit(
    *, project: str, filename: str, content: bytes, content_type: str, language: str | None
) -> dict[str, Any]:
    """Upload audio to the Transcriber; store and return the local job record."""
    files = {"audio": (filename, content, content_type or "application/octet-stream")}
    data = {}
    if language:
        data["language"] = language
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{_base_url()}/api/v1/transcriptions",
            headers=_auth_headers(),
            files=files,
            data=data,
        )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"transcriber submit failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    record = {
        "job_id": body["id"],
        "status": body.get("status", "queued"),
        "filename": filename,
        "language": language,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "transcript_file": None,
    }
    _save_job(project, record)
    return record


async def status(*, project: str, job_id: str) -> dict[str, Any]:
    """Poll the Transcriber; on completion persist the transcript once."""
    record = _load_job(project, job_id)
    if record is None:
        raise LookupError(job_id)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{_base_url()}/api/v1/transcriptions/{job_id}", headers=_auth_headers()
        )
    if resp.status_code == 404:
        raise LookupError(job_id)
    if resp.status_code != 200:
        raise RuntimeError(f"transcriber status failed ({resp.status_code}): {resp.text[:300]}")
    remote = resp.json()

    record["status"] = remote.get("status", record["status"])
    record["progress"] = remote.get("progress", 0)
    record["detected_language"] = remote.get("detected_language")
    record["duration_seconds"] = remote.get("duration_seconds")
    record["error"] = remote.get("error")

    # Persist the finished transcript exactly once, into the project transcripts dir.
    if remote.get("status") == "completed" and not record.get("transcript_file"):
        text = remote.get("text") or ""
        title = Path(record["filename"]).stem or "recording"
        stored = transcripts_service.store(
            project=project,
            title=title,
            content=text,
            source=f"transcriber:{job_id}",
        )
        record["transcript_file"] = stored["filename"]
        record["transcript_path"] = stored["path"]

    _save_job(project, record)
    return {**record, "segments": remote.get("segments") if remote.get("status") == "completed" else None,
            "text": remote.get("text") if remote.get("status") == "completed" else None}


def list_jobs(project: str) -> list[dict[str, Any]]:
    try:
        d = _jobs_dir(project)
    except LookupError:
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out
