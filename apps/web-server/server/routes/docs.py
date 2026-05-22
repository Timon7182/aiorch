"""Documentation generation + viewer routes.

Endpoints:
    POST /api/projects/{project_id}/docs/generate
        Kick off the doc-generator agent. Returns immediately; client polls
        /status (or listens for the existing task websocket events).
    GET  /api/projects/{project_id}/docs/status
        {state, last_build, last_build_ok, files} for the project's docs.
    POST /api/projects/{project_id}/docs/build
        Re-run `mkdocs build` without invoking the agent (use after manual
        markdown edits).
    GET  /api/projects/{project_id}/docs/tree
        List markdown files in <project>/docs/ for a sidebar tree.
    GET  /api/projects/{project_id}/docs/site/{path:path}
        Serve a file from <project>/.magestic-ai/docs-site/. Path-sanitized
        so the user can't escape the directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from pathlib import Path as FilePath

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, Response

from ..services.docs_generator_service import get_docs_generator_service
from .projects import load_projects

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_project(project_id: str) -> Path:
    projects = load_projects()
    pdata = projects.get(project_id)
    if not pdata:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} not registered",
        )
    project_path = FilePath(pdata["path"])
    if not project_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Project path no longer exists: {project_path}",
        )
    return project_path


def _backend_path() -> FilePath:
    """Locate apps/backend relative to apps/web-server (where we run)."""
    # apps/web-server/server/routes/docs.py → apps/backend
    return FilePath(__file__).resolve().parents[3] / "backend"


async def _resolve_oauth_token() -> str | None:
    """Get a freshly-refreshed OAuth token from ClaudeTokenService, falling
    back to the static env var if the service is unavailable."""
    try:
        from ..services.claude_token_service import get_claude_token_service
        token = await get_claude_token_service().get_access_token()
        if token:
            return token
    except Exception:
        pass
    import os
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None


async def _resolve_user_identity(raw_request: Request) -> tuple[str, str] | None:
    user = getattr(raw_request.state, "user", None)
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    if not user_id:
        return None
    try:
        from sqlalchemy import select
        from ..database.engine import async_session_factory
        from ..database.models import User
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(User.name, User.email).where(User.id == user_id)
                )
            ).first()
            if row and row.name and row.email:
                return row.name, row.email
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


@router.post("/{project_id}/docs/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_docs(project_id: str, raw_request: Request):
    """Spawn the doc-generator agent in the background; return immediately."""
    project_path = _resolve_project(project_id)
    svc = get_docs_generator_service(_backend_path())

    if svc.is_running(project_id):
        return {
            "success": False,
            "state": "running",
            "error": "Documentation generation is already in progress for this project.",
        }

    user_identity = await _resolve_user_identity(raw_request)
    oauth_token = await _resolve_oauth_token()

    # Fire-and-forget: the agent writes to disk; the UI polls /status.
    async def _run():
        try:
            result = await svc.generate(
                project_id=project_id,
                project_path=project_path,
                oauth_token=oauth_token,
                user_identity=user_identity,
            )
            logger.info(
                f"[docs] generation finished for {project_id}: "
                f"ok={result.success}, files={len(result.files_generated)}, "
                f"error={result.error}"
            )
        except Exception:
            logger.exception(f"[docs] generation crashed for {project_id}")

    asyncio.create_task(_run())
    return {"success": True, "state": "started"}


@router.post("/{project_id}/docs/build")
async def build_docs(project_id: str):
    """Re-run `mkdocs build` without the agent."""
    project_path = _resolve_project(project_id)
    svc = get_docs_generator_service(_backend_path())
    log, ok = await svc.build_only(project_path)
    return {"success": ok, "log": log[-4000:]}


@router.get("/{project_id}/docs/status")
async def docs_status(project_id: str):
    project_path = _resolve_project(project_id)
    svc = get_docs_generator_service(_backend_path())

    docs_dir = project_path / "docs"
    site_dir = project_path / ".magestic-ai" / "docs-site"
    marker = project_path / ".magestic-ai" / ".docgen.json"

    meta: dict = {}
    if marker.exists():
        try:
            meta = json.loads(marker.read_text())
        except json.JSONDecodeError:
            meta = {}

    return {
        "success": True,
        "state": "running" if svc.is_running(project_id) else "idle",
        "has_docs": docs_dir.is_dir(),
        "has_site": (site_dir / "index.html").exists(),
        "last_run": meta.get("last_run"),
        "last_build": meta.get("last_build"),
        "last_build_ok": meta.get("last_build_ok"),
        "head_sha": meta.get("head_sha"),
    }


# ---------------------------------------------------------------------------
# Browse + serve
# ---------------------------------------------------------------------------


@router.get("/{project_id}/docs/tree")
async def docs_tree(project_id: str):
    """List markdown files under <project>/docs/ for a sidebar tree."""
    project_path = _resolve_project(project_id)
    docs_dir = project_path / "docs"
    if not docs_dir.is_dir():
        return {"success": True, "files": []}
    files: list[dict] = []
    for path in sorted(docs_dir.rglob("*.md")):
        rel = path.relative_to(docs_dir).as_posix()
        files.append({"path": rel, "size": path.stat().st_size})
    return {"success": True, "files": files}


@router.get("/{project_id}/docs/raw")
async def docs_raw_markdown(project_id: str, path: str):
    """Return the raw markdown source of a doc file (for in-app editing)."""
    project_path = _resolve_project(project_id)
    docs_dir = (project_path / "docs").resolve()
    target = (docs_dir / path).resolve()
    # Path-traversal guard.
    try:
        target.relative_to(docs_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes docs directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return {
        "success": True,
        "path": path,
        "content": target.read_text(encoding="utf-8", errors="replace"),
    }


@router.get("/{project_id}/docs/site/{path:path}")
async def docs_site_serve(project_id: str, path: str):
    """Serve a built file from <project>/.magestic-ai/docs-site/."""
    project_path = _resolve_project(project_id)
    site_dir = (project_path / ".magestic-ai" / "docs-site").resolve()
    # Default to index.html when path is empty or ends with /.
    resolved_path = path if path else "index.html"
    if resolved_path.endswith("/"):
        resolved_path = resolved_path + "index.html"
    target = (site_dir / resolved_path).resolve()
    try:
        target.relative_to(site_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes site directory")
    if not target.is_file():
        # MkDocs builds directory-style URLs (e.g. /api/ -> api/index.html).
        # Try the index.html variant before giving up.
        index_variant = (target / "index.html").resolve()
        try:
            index_variant.relative_to(site_dir)
        except ValueError:
            raise HTTPException(status_code=400, detail="Path escapes site directory")
        if index_variant.is_file():
            target = index_variant
        else:
            raise HTTPException(status_code=404, detail="File not found in built site")
    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(target, media_type=media_type or "application/octet-stream")
