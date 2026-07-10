"""Project-extension routes layered on top of MagesticAI: servers, DBs, transcripts.

The user-facing additions for the project: a place to manage SSH targets for
log/deploy ops, multi-env DB connection profiles, and meeting transcripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import (
    db_service,
    docs_index_service,
    ext_storage,
    ssh_service,
    transcripts_service,
)

router = APIRouter()


# ---------- Servers (SSH) ----------


class ServerProfile(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str
    auth_method: str = Field(default="password", pattern="^(password|key)$")
    password: str | None = None
    private_key_path: str | None = None
    logs: dict[str, str] = Field(default_factory=dict)
    deploys: dict[str, str] = Field(default_factory=dict)
    # Container->host path prefixes for preview deploys, e.g.
    # {"/home/magesticai/projects": "/home/saya/projects"}. Used by
    # preview_deploy_service to translate worktree/config paths the runner sees.
    host_path_map: dict[str, str] = Field(default_factory=dict)
    project: str | None = None


@router.get("/servers", tags=["Servers"])
async def list_servers() -> list[dict[str, Any]]:
    return [_redact_server(s) for s in ext_storage.load("servers")]


@router.post("/servers", tags=["Servers"])
async def create_server(profile: ServerProfile) -> dict[str, Any]:
    stored = ext_storage.insert("servers", profile.model_dump())
    return _redact_server(stored)


@router.delete("/servers/{server_id}", tags=["Servers"])
async def delete_server(server_id: str) -> dict[str, str]:
    if not ext_storage.delete("servers", server_id):
        raise HTTPException(status_code=404, detail="server not found")
    return {"status": "deleted"}


@router.post("/servers/{server_id}/test", tags=["Servers"])
async def test_server(server_id: str) -> dict[str, Any]:
    profile = _require_server(server_id)
    try:
        return ssh_service.test_connection(profile)
    except ssh_service.SshError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class TailRequest(BaseModel):
    log_name: str
    lines: int = 200


@router.post("/servers/{server_id}/tail", tags=["Servers"])
async def tail_log_on_server(server_id: str, req: TailRequest) -> dict[str, Any]:
    """Tail an allowlisted log on the server. log_name must exist in profile.logs."""
    profile = _require_server(server_id)
    try:
        res = ssh_service.tail_log(profile, req.log_name, lines=req.lines)
    except ssh_service.SshError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code}


class DeployRequest(BaseModel):
    deploy_name: str


@router.post("/servers/{server_id}/deploy", tags=["Servers"])
async def deploy_on_server(server_id: str, req: DeployRequest) -> dict[str, Any]:
    """Run an allowlisted deploy script on the server. deploy_name must exist in profile.deploys."""
    profile = _require_server(server_id)
    try:
        res = ssh_service.run_deploy(profile, req.deploy_name)
    except ssh_service.SshError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code}


def _require_server(server_id: str) -> dict[str, Any]:
    p = ext_storage.find("servers", server_id)
    if not p:
        raise HTTPException(status_code=404, detail="server not found")
    return p


def _redact_server(s: dict[str, Any]) -> dict[str, Any]:
    out = dict(s)
    if out.get("password"):
        out["password"] = "***"
    return out


# ---------- Databases ----------


class DbProfile(BaseModel):
    name: str
    kind: str = Field(pattern="^(postgres|mysql|sqlite)$")
    env: str = "dev"
    host: str = "localhost"
    port: int | None = None
    database: str
    username: str | None = None
    password: str | None = None
    project: str | None = None
    # Optional per-project scoping: when non-empty, this profile is only offered
    # in the chat DB selector for the listed project ids. Absent/empty = global
    # (visible everywhere) — no migration needed for existing profiles.
    projectIds: list[str] = Field(default_factory=list)


@router.get("/databases", tags=["Databases"])
async def list_databases() -> list[dict[str, Any]]:
    return [_redact_db(d) for d in ext_storage.load("databases")]


@router.post("/databases", tags=["Databases"])
async def create_database(profile: DbProfile) -> dict[str, Any]:
    stored = ext_storage.insert("databases", profile.model_dump())
    return _redact_db(stored)


@router.delete("/databases/{db_id}", tags=["Databases"])
async def delete_database(db_id: str) -> dict[str, str]:
    if not ext_storage.delete("databases", db_id):
        raise HTTPException(status_code=404, detail="database not found")
    return {"status": "deleted"}


@router.post("/databases/{db_id}/test", tags=["Databases"])
async def test_database(db_id: str) -> dict[str, Any]:
    profile = _require_db(db_id)
    return db_service.test_connection(profile)


class QueryRequest(BaseModel):
    sql: str
    limit: int = 200
    allow_writes: bool = False


@router.post("/databases/{db_id}/query", tags=["Databases"])
async def query_database(db_id: str, req: QueryRequest) -> dict[str, Any]:
    profile = _require_db(db_id)
    try:
        return db_service.run_query(profile, req.sql, limit=req.limit, allow_writes=req.allow_writes)
    except db_service.DbError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/databases/{db_id}/schema", tags=["Databases"])
async def db_schema(db_id: str) -> dict[str, Any]:
    profile = _require_db(db_id)
    try:
        return db_service.introspect_schema(profile)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _require_db(db_id: str) -> dict[str, Any]:
    p = ext_storage.find("databases", db_id)
    if not p:
        raise HTTPException(status_code=404, detail="database not found")
    return p


def _redact_db(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    if out.get("password"):
        out["password"] = "***"
    return out


# ---------- Transcripts ----------


class TranscriptUpload(BaseModel):
    project: str
    title: str
    content: str
    occurred_at: str | None = None
    participants: list[str] | None = None
    source: str | None = None


@router.post("/transcripts", tags=["Transcripts"])
async def upload_transcript(req: TranscriptUpload) -> dict[str, Any]:
    try:
        return transcripts_service.store(
            project=req.project,
            title=req.title,
            content=req.content,
            occurred_at=req.occurred_at,
            participants=req.participants,
            source=req.source,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"project {req.project!r} not registered or path missing",
        ) from exc


@router.get("/transcripts/{project}", tags=["Transcripts"])
async def list_transcripts(project: str) -> list[dict[str, Any]]:
    return transcripts_service.list_for(project)


@router.get("/transcripts/{project}/{filename}", tags=["Transcripts"])
async def read_transcript(project: str, filename: str) -> dict[str, Any]:
    try:
        content = transcripts_service.read(project, filename)
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"project {project!r} not registered or path missing",
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="transcript not found") from exc
    return {"filename": filename, "content": content}


# ---------- Docs Index ----------


class DocsReindexRequest(BaseModel):
    project: str
    root_dir: str


@router.post("/docs-index/reindex", tags=["Docs Index"])
async def reindex_docs(req: DocsReindexRequest) -> dict[str, Any]:
    try:
        return docs_index_service.reindex(req.project, Path(req.root_dir))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/docs-index/{project}/search", tags=["Docs Index"])
async def search_docs(project: str, q: str, limit: int = 20) -> list[dict[str, Any]]:
    return docs_index_service.search(project, q, limit=limit)


@router.get("/docs-index/{project}/stats", tags=["Docs Index"])
async def docs_index_stats(project: str) -> dict[str, Any]:
    return docs_index_service.stats(project)
