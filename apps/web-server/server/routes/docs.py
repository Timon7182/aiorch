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
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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
    project_path = Path(pdata["path"])
    if not project_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Project path no longer exists: {project_path}",
        )
    return project_path


async def _resolve_docs_base(
    project_id: str, repo: str | None, branch: str | None = None
) -> Path:
    """Directory that docs/graphify-out live in for a project.

    For multi-repo projects (a parent folder of child repos), ``repo`` selects
    which child repo's docs to use; it is validated against the project's
    detected repos to prevent path traversal. Single-repo projects resolve to
    the project root and ignore ``repo``.

    When ``branch`` is set and differs from the repo's current checkout, the
    docs base becomes a read-only branch worktree (the SAME mechanism the
    insights chat uses, :func:`ensure_branch_worktree`) so generate/tree/raw/
    build/site/codegraph all operate inside that branch's tree without touching
    the user's working copy. An empty/unknown branch, or one already checked
    out, falls back to the current HEAD — fully backward compatible.

    Note: docs branch-worktrees share the insights worktree pool
    (.magestic-ai/worktrees/insights/<branch>), which is LRU-capped at 5 per
    repo (see branch_worktree.cleanup_insights_worktrees). An idle branch's
    worktree — including any docs generated into it — can therefore be evicted;
    the next request for that branch transparently recreates the worktree from
    the branch tip (docs committed to the branch survive; uncommitted generated
    docs in the worktree do not).
    """
    project_path = _resolve_project(project_id)
    from ..services.git_repos import resolve_repo_cwd
    base = Path(resolve_repo_cwd(str(project_path), repo))
    if branch and branch.strip():
        from ..services.branch_worktree import ensure_branch_worktree
        # ensure_branch_worktree shells out to git; run it off the event loop.
        worktree = await asyncio.to_thread(ensure_branch_worktree, base, branch.strip())
        if worktree is not None:
            return Path(worktree)
    return base


def _backend_path() -> Path:
    """Locate apps/backend relative to apps/web-server (where we run)."""
    # apps/web-server/server/routes/docs.py → apps/backend
    return Path(__file__).resolve().parents[3] / "backend"


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
async def generate_docs(
    project_id: str,
    raw_request: Request,
    repo: str | None = None,
    template: str | None = None,
    branch: str | None = None,
):
    """Spawn the doc-generator agent in the background; return immediately.

    ``template`` selects the doc template (structure/mkdocs/page layout);
    defaults to "default" in the runner when omitted. ``branch`` generates the
    docs inside a read-only worktree of that branch (empty => current checkout).
    """
    project_path = await _resolve_docs_base(project_id, repo, branch)
    svc = get_docs_generator_service(_backend_path())

    if svc.is_running(project_id):
        # Error shape: api-client preserves {success: false, error: ...} as-is.
        return {
            "success": False,
            "error": "Documentation generation is already in progress for this project.",
        }

    user_identity = await _resolve_user_identity(raw_request)
    oauth_token = await _resolve_oauth_token()

    # Set the starting flag synchronously so any /status call that races
    # ahead of the spawn (the create_task below) still sees state=running.
    svc.mark_starting(project_id)

    # Fire-and-forget: the agent writes to disk; the UI polls /status.
    async def _run():
        try:
            result = await svc.generate(
                project_id=project_id,
                project_path=project_path,
                oauth_token=oauth_token,
                user_identity=user_identity,
                template=template,
                branch=branch,
            )
            logger.info(
                f"[docs] generation finished for {project_id}: "
                f"ok={result.success}, files={len(result.files_generated)}, "
                f"error={result.error}"
            )
        except Exception:
            logger.exception(f"[docs] generation crashed for {project_id}")

    asyncio.create_task(_run())
    # Raw success payload; api-client wraps to {success: true, data: {state: ...}}
    return {"state": "started"}


@router.post("/{project_id}/docs/cancel")
async def cancel_docs_generation(project_id: str):
    """Stop the running doc generator for this project (if any)."""
    _resolve_project(project_id)  # 404 if project unknown
    svc = get_docs_generator_service(_backend_path())
    cancelled = await svc.cancel(project_id)
    if not cancelled:
        return {"success": False, "error": "No generation in progress."}
    return {"state": "cancelled"}


@router.post("/{project_id}/docs/build")
async def build_docs(project_id: str, repo: str | None = None, branch: str | None = None):
    """Re-run `mkdocs build` without the agent."""
    project_path = await _resolve_docs_base(project_id, repo, branch)
    svc = get_docs_generator_service(_backend_path())
    log, ok = await svc.build_only(project_path)
    if not ok:
        return {"success": False, "error": log[-4000:]}
    return {"log": log[-4000:]}


_HOOK_BEGIN = "# >>> magestic-docs >>>"
_HOOK_END = "# <<< magestic-docs <<<"
# The touch-file block. Portable across POSIX sh and Git-for-Windows' bundled
# sh (git runs hooks under sh on Windows too). `: > file` creates/empties the
# marker without relying on `touch`; failures are swallowed so a commit never
# breaks over docs bookkeeping.
_HOOK_BLOCK = (
    f"{_HOOK_BEGIN}\n"
    "# Requests a MagesticAI docs refresh after each commit (honored by the\n"
    "# optional docs watcher, DOCS_WATCH_ENABLED=true).\n"
    'root="$(git rev-parse --show-toplevel 2>/dev/null)"\n'
    'if [ -n "$root" ]; then\n'
    '  mkdir -p "$root/.magestic-ai" 2>/dev/null || true\n'
    '  : > "$root/.magestic-ai/.docs-refresh-requested" 2>/dev/null || true\n'
    "fi\n"
    f"{_HOOK_END}\n"
)


@router.post("/{project_id}/docs/install-hook")
async def install_docs_hook(project_id: str, repo: str | None = None):
    """Install an idempotent post-commit hook that requests a docs refresh.

    The hook only ever writes ``.magestic-ai/.docs-refresh-requested``; the
    optional docs watcher (DOCS_WATCH_ENABLED) picks that up and runs a
    change-aware regeneration. Always targets the *current checkout's* repo
    (never a branch worktree), so ``branch`` is intentionally not accepted here.

    Idempotent: if a post-commit hook already exists, the magestic block is
    appended between clear markers (and re-installing replaces just that block),
    so a user's own hook logic is preserved.
    """
    # repo (multi-repo) is honored; branch is not — hooks live in the real repo.
    project_path = await _resolve_docs_base(project_id, repo, None)

    # Resolve the hooks dir robustly (handles worktrees / non-standard gitdir).
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "safe.directory=*", "rev-parse", "--git-path", "hooks",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError) as e:
        return {"success": False, "error": f"Could not locate git hooks dir: {e}"}
    if proc.returncode != 0:
        return {"success": False, "error": "Not a git repository (no hooks dir)."}

    rel = out.decode("utf-8", "replace").strip() or ".git/hooks"
    hooks_dir = (project_path / rel) if not Path(rel).is_absolute() else Path(rel)
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"success": False, "error": f"Could not create hooks dir: {e}"}

    hook_path = hooks_dir / "post-commit"
    try:
        if hook_path.exists():
            existing = hook_path.read_text(encoding="utf-8", errors="replace")
            if _HOOK_BEGIN in existing and _HOOK_END in existing:
                # Replace just our marked block so re-install stays idempotent.
                pre = existing.split(_HOOK_BEGIN, 1)[0].rstrip("\n")
                post = existing.split(_HOOK_END, 1)[1].lstrip("\n")
                new_content = f"{pre}\n\n{_HOOK_BLOCK}" if pre else _HOOK_BLOCK
                if post:
                    new_content += f"\n{post}"
            else:
                # Append our block to the user's existing hook.
                sep = "" if existing.endswith("\n") else "\n"
                new_content = f"{existing}{sep}\n{_HOOK_BLOCK}"
            action = "updated"
        else:
            new_content = f"#!/bin/sh\n{_HOOK_BLOCK}"
            action = "installed"
        hook_path.write_text(new_content, encoding="utf-8")
        # chmod +x on POSIX; no-op semantics on Windows (git runs it under sh).
        try:
            import stat as _stat
            mode = hook_path.stat().st_mode
            hook_path.chmod(mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
        except OSError:
            pass
    except OSError as e:
        return {"success": False, "error": f"Could not write post-commit hook: {e}"}

    return {"state": action, "hook_path": str(hook_path)}


# ---------------------------------------------------------------------------
# Doc templates
# ---------------------------------------------------------------------------

_TEMPLATE_DIRNAME = "doc-templates"
# Slug guard: also prevents path traversal via the {name} path param.
_TEMPLATE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _builtin_templates_dir() -> Path:
    return _backend_path() / "prompts" / "doc_templates"


def _global_templates_dir() -> Path:
    return Path.home() / ".magestic-ai" / _TEMPLATE_DIRNAME


def _project_templates_dir(project_path: Path) -> Path:
    return project_path / ".magestic-ai" / _TEMPLATE_DIRNAME


def _read_template_manifest(template_dir: Path) -> dict | None:
    manifest = template_dir / "template.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _enumerate_templates(project_path: Path) -> dict[str, dict]:
    """Merge templates from builtin → global → project (later wins)."""
    merged: dict[str, dict] = {}
    for source, base in (
        ("builtin", _builtin_templates_dir()),
        ("global", _global_templates_dir()),
        ("project", _project_templates_dir(project_path)),
    ):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            data = _read_template_manifest(child)
            if data is None:
                continue
            name = str(data.get("name") or child.name)
            merged[name] = {
                "name": name,
                "description": str(data.get("description") or ""),
                "source": source,
            }
    return merged


class DocsTemplateBody(BaseModel):
    """Body for PUT /docs/templates/{name} — a template manifest."""

    name: str | None = None  # ignored; the path param is authoritative
    description: str
    structure: str
    mkdocs_yml: str
    page_templates: str
    extra_instructions: str | None = ""
    repo: str | None = None


@router.get("/{project_id}/docs/templates")
async def list_docs_templates(project_id: str, repo: str | None = None):
    """List all doc templates available to this project (builtin/global/project)."""
    # Templates are branch-independent (they live under .magestic-ai/, which is
    # gitignored), so branch=None here.
    project_path = await _resolve_docs_base(project_id, repo, None)
    merged = _enumerate_templates(project_path)
    return sorted(merged.values(), key=lambda t: t["name"])


@router.put("/{project_id}/docs/templates/{name}")
async def save_docs_template(project_id: str, name: str, body: DocsTemplateBody):
    """Create or update a project-level doc template."""
    project_path = await _resolve_docs_base(project_id, body.repo, None)
    if not _TEMPLATE_SLUG_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Invalid template name (lowercase letters, digits, '-' or '_')",
        )
    manifest = {
        "name": name,
        "description": body.description,
        "structure": body.structure,
        "mkdocs_yml": body.mkdocs_yml,
        "page_templates": body.page_templates,
        "extra_instructions": body.extra_instructions or "",
    }
    dest = _project_templates_dir(project_path) / name
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "template.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")
    return {"ok": True}


@router.delete("/{project_id}/docs/templates/{name}")
async def delete_docs_template(project_id: str, name: str, repo: str | None = None):
    """Delete a project-level doc template. Builtin/global are read-only (404)."""
    project_path = await _resolve_docs_base(project_id, repo, None)
    if not _TEMPLATE_SLUG_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid template name")
    dest = _project_templates_dir(project_path) / name
    if not (dest / "template.json").is_file():
        raise HTTPException(
            status_code=404, detail="No project-level template with that name"
        )
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")
    return {"ok": True}


@router.post("/{project_id}/docs/codegraph/index", status_code=status.HTTP_202_ACCEPTED)
async def index_codegraph(project_id: str, repo: str | None = None, branch: str | None = None):
    """Build/refresh the CodeGraphContext index for the project (background).

    Runs `codegraphcontext index <path> --force`, creating `.codegraphcontext/`
    — the folder that gates the CGC MCP tools (find_code,
    analyze_code_relationships, find_dead_code, complexity, ...) for the
    planner/coder/QA agents. Tree-sitter only: no LLM, no API key. Returns
    immediately; poll /docs/status (codegraph_indexing → has_codegraph /
    last_codegraph) for progress. This is an explicit user action and so runs
    regardless of the CGC_AUTO_INDEX flag.
    """
    project_path = await _resolve_docs_base(project_id, repo, branch)
    svc = get_docs_generator_service(_backend_path())

    if svc.is_indexing(project_path):
        return {
            "success": False,
            "error": "Code graph indexing is already in progress for this project.",
        }

    async def _run():
        try:
            await svc.index_codegraph(project_path)
        except Exception:
            logger.exception(f"[docs] codegraph index crashed for {project_id}")

    asyncio.create_task(_run())
    return {"state": "started"}


@router.get("/{project_id}/docs/status")
async def docs_status(project_id: str, repo: str | None = None, branch: str | None = None):
    project_path = await _resolve_docs_base(project_id, repo, branch)
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

    graphify_out = project_path / "graphify-out"
    # Raw payload (no success wrapper). api-client wraps to
    # {success: true, data: <this>}, and DocumentationView reads r.data.*.
    # Returning a top-level success field would trip the client's
    # double-wrap detection and leave r.data undefined.
    return {
        "state": "running" if svc.is_running(project_id) else "idle",
        "has_docs": docs_dir.is_dir(),
        "has_site": (site_dir / "index.html").exists(),
        "has_graph": (graphify_out / "graph.json").exists(),
        "has_codegraph": (project_path / ".codegraphcontext").is_dir(),
        "codegraph_indexing": svc.is_indexing(project_path),
        "last_run": meta.get("last_run"),
        "last_build": meta.get("last_build"),
        "last_build_ok": meta.get("last_build_ok"),
        "last_graphify": meta.get("last_graphify"),
        "last_codegraph": meta.get("last_codegraph"),
        "head_sha": meta.get("head_sha"),
        "branch": meta.get("branch"),
    }


# ---------------------------------------------------------------------------
# Browse + serve
# ---------------------------------------------------------------------------


@router.get("/{project_id}/docs/tree")
async def docs_tree(project_id: str, repo: str | None = None, branch: str | None = None):
    """List markdown files under <project>/docs/ for a sidebar tree."""
    project_path = await _resolve_docs_base(project_id, repo, branch)
    docs_dir = project_path / "docs"
    if not docs_dir.is_dir():
        return {"files": []}
    files: list[dict] = []
    for path in sorted(docs_dir.rglob("*.md")):
        rel = path.relative_to(docs_dir).as_posix()
        files.append({"path": rel, "size": path.stat().st_size})
    return {"files": files}


@router.get("/{project_id}/docs/raw")
async def docs_raw_markdown(
    project_id: str, path: str, repo: str | None = None, branch: str | None = None
):
    """Return the raw markdown source of a doc file (for in-app editing)."""
    project_path = await _resolve_docs_base(project_id, repo, branch)
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
        "path": path,
        "content": target.read_text(encoding="utf-8", errors="replace"),
    }


class DocsWriteBody(BaseModel):
    """Body for PUT /docs/raw — save a hand-edited doc file."""

    path: str
    # 5 MB cap: generous for markdown, blocks accidental/abusive huge payloads.
    content: str = Field(..., max_length=5_000_000)
    repo: str | None = None
    # Optional branch: edit the copy inside that branch's read-only docs
    # worktree (same resolution as GET /docs/raw). None => current checkout.
    branch: str | None = None


@router.put("/{project_id}/docs/raw")
async def docs_raw_write(project_id: str, body: DocsWriteBody):
    """Overwrite a markdown doc file (or mkdocs.yml) with user-edited content.

    Companion write endpoint to GET /docs/raw, used by the in-app editor.
    Only markdown files under ``docs/`` and the project-root ``mkdocs.yml``
    may be written; the same path-traversal guard as the GET applies. Does
    NOT rebuild the site — the client calls POST /docs/build explicitly.
    When ``branch`` is set, the write lands in that branch's docs worktree so
    manual edits work on branch-scoped docs too.
    """
    project_path = await _resolve_docs_base(project_id, body.repo, body.branch)
    rel = (body.path or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="Missing path")

    # mkdocs.yml is a special case: it lives at the repo root, not under docs/.
    if rel in ("mkdocs.yml", "./mkdocs.yml"):
        target = (project_path / "mkdocs.yml").resolve()
        base = project_path.resolve()
    else:
        docs_dir = (project_path / "docs").resolve()
        target = (docs_dir / rel).resolve()
        # Path-traversal guard (mirrors the GET).
        try:
            target.relative_to(docs_dir)
        except ValueError:
            raise HTTPException(status_code=400, detail="Path escapes docs directory")
        if target.suffix.lower() != ".md":
            raise HTTPException(status_code=400, detail="Only .md files may be edited")
        base = docs_dir

    # Defense in depth: re-confirm the resolved target stays under its base.
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes allowed directory")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")
    return {"ok": True}


@router.get("/{project_id}/docs/graph-report")
async def docs_graph_report(project_id: str, repo: str | None = None, branch: str | None = None):
    """Return the markdown content of <project>/graphify-out/GRAPH_REPORT.md.

    Mirrors the shape of /docs/raw so the frontend can drop the result
    straight into its existing markdown viewer.
    """
    project_path = await _resolve_docs_base(project_id, repo, branch)
    report = project_path / "graphify-out" / "GRAPH_REPORT.md"
    if not report.is_file():
        raise HTTPException(
            status_code=404,
            detail="No graph report. Run `graphify extract` first.",
        )
    return {
        "path": "GRAPH_REPORT.md",
        "content": report.read_text(encoding="utf-8", errors="replace"),
    }


@router.get("/{project_id}/docs/codegraph-report")
async def docs_codegraph_report(
    project_id: str, repo: str | None = None, refresh: bool = False, branch: str | None = None
):
    """Return CodeGraphContext's CGC_REPORT.md (god nodes, complexity hotspots,
    cross-module links, suggested queries) for the project's code graph.

    Generated on demand from the repo's `.codegraphcontext/` graph and cached
    in `.codegraphcontext/CGC_REPORT.md`. Mirrors /docs/graph-report's shape so
    the frontend drops it straight into its markdown viewer. Pass refresh=true
    to rebuild after re-indexing.
    """
    project_path = await _resolve_docs_base(project_id, repo, branch)
    svc = get_docs_generator_service(_backend_path())
    content = await svc.codegraph_report(project_path, refresh=refresh)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail="No code graph. Run `codegraphcontext index <path>` first.",
        )
    return {"path": "CGC_REPORT.md", "content": content}


@router.get("/{project_id}/docs/graph/{path:path}")
async def docs_graph_serve(
    project_id: str, path: str, repo: str | None = None, branch: str | None = None
):
    """Serve any file from <project>/graphify-out/ (graph.html, graph.json, etc.).

    Path-sanitized so the user can't escape the directory. Used to embed
    the interactive graph.html viewer and to download graph.json.
    """
    project_path = await _resolve_docs_base(project_id, repo, branch)
    graph_dir = (project_path / "graphify-out").resolve()
    resolved_path = path if path else "graph.html"
    target = (graph_dir / resolved_path).resolve()
    try:
        target.relative_to(graph_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes graph directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in graph directory")
    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(target, media_type=media_type or "application/octet-stream")


@router.get("/{project_id}/docs/site/{path:path}")
async def docs_site_serve(
    project_id: str, path: str, repo: str | None = None, branch: str | None = None
):
    """Serve a built file from <project>/.magestic-ai/docs-site/."""
    project_path = await _resolve_docs_base(project_id, repo, branch)
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
