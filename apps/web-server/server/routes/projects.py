"""
Project management routes.

Handles CRUD operations for projects (git repositories that Magestic AI manages).
"""

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Type Definitions for Validation
# --------------------------------------------------------------------------

# BUG-1.2-003: Memory backend must be one of these values
MemoryBackendType = Literal["graphiti", "file"]

from ..config import get_settings
from . import changelog, context, files, git, github
from .git import run_git_command

logger = logging.getLogger(__name__)

router = APIRouter()


def _kickoff_codegraph_index(project_path: str) -> None:
    """Fire-and-forget: build the CodeGraphContext index for a newly registered
    project so the planner/coder/QA agents get CGC's code-graph MCP tools
    without a manual indexing step.

    No-ops when CGC auto-indexing is disabled (CGC_AUTO_INDEX=false /
    CODEGRAPH_DISABLED=true) or the `codegraphcontext` CLI isn't installed.
    Never raises into the request path — project registration must succeed
    regardless of indexing.
    """
    try:
        from ..services.docs_generator_service import get_docs_generator_service

        backend_path = Path(get_settings().BACKEND_PATH)
        svc = get_docs_generator_service(backend_path)
        if not svc.auto_index_enabled():
            return
        asyncio.create_task(svc.index_codegraph(Path(project_path)))
    except Exception:
        logger.debug("Failed to kick off CGC index for %s", project_path, exc_info=True)


def _docs_auto_generate_enabled() -> bool:
    """Whether docs are auto-generated on project creation.

    Off by default — doc generation spends LLM tokens, so it's opt-in via
    DOCS_AUTO_GENERATE_ON_CREATE=true. CGC indexing (free, tree-sitter) stays on.
    """
    return str(os.environ.get("DOCS_AUTO_GENERATE_ON_CREATE", "")).lower() in (
        "true",
        "1",
        "yes",
    )


def _kickoff_docs_generation(project_id: str, project_path: str) -> None:
    """Fire-and-forget full docs generation for a newly created project.

    Gated on DOCS_AUTO_GENERATE_ON_CREATE (default off). Never raises into the
    request path — registration must succeed regardless of doc generation.
    """
    if not _docs_auto_generate_enabled():
        return
    try:
        from ..services.docs_generator_service import get_docs_generator_service

        backend_path = Path(get_settings().BACKEND_PATH)
        svc = get_docs_generator_service(backend_path)

        async def _run():
            try:
                token = await svc.resolve_oauth_token()
                await svc.generate(
                    project_id=project_id,
                    project_path=Path(project_path),
                    oauth_token=token,
                )
            except Exception:
                logger.debug(
                    "Auto docs generation failed for %s", project_path, exc_info=True
                )

        # Mirror the interactive /docs/generate route: flag the project as
        # starting *before* scheduling the task so a /docs/status call that
        # races the spawn already reports state=running.
        svc.mark_starting(project_id)
        asyncio.create_task(_run())
    except Exception:
        logger.debug(
            "Failed to kick off docs generation for %s", project_path, exc_info=True
        )


# Include project-specific sub-routers
# These will be available under /api/projects/{projectId}/...
router.include_router(
    github.project_router, prefix="/{projectId}/github", tags=["GitHub"]
)
router.include_router(
    changelog.router, prefix="/{projectId}/changelog", tags=["Changelog"]
)
router.include_router(
    changelog.insights_router, prefix="/{projectId}/insights", tags=["Insights"]
)
router.include_router(
    files.insights_router, prefix="/{projectId}/files/insights", tags=["Files Insights"]
)
router.include_router(context.project_router, prefix="/{projectId}", tags=["Context"])
router.include_router(git.project_router, prefix="", tags=["Git"])
router.include_router(
    git.releases_router, prefix="/{projectId}/releases", tags=["Releases"]
)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class ProjectBase(BaseModel):
    """Base project model."""

    path: str = Field(..., description="Absolute path to the project directory")
    name: str | None = Field(None, description="Display name for the project")


class ProjectCreate(ProjectBase):
    """Model for creating a new project."""

    pass


class NotificationSettings(BaseModel):
    """Notification settings model - BUG-1.2-004: Now properly typed."""

    onTaskComplete: bool = Field(default=True)
    onTaskFailed: bool = Field(default=True)
    onReviewNeeded: bool = Field(default=True)
    sound: bool = Field(default=True)
    emailEnabled: bool = Field(default=False)


class ProjectSettings(BaseModel):
    """Project settings model matching frontend expectations."""

    model_config = ConfigDict(populate_by_name=True)

    model: str = Field(default="claude-sonnet-4-5-20250929")
    # BUG-1.2-003: Validate memoryBackend against allowed values
    memoryBackend: MemoryBackendType = Field(default="file", alias="memory_backend")
    # BUG-1.2-004: notifications now properly typed
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    graphitiMcpEnabled: bool = Field(default=False, alias="graphiti_mcp_enabled")
    graphitiMcpUrl: str | None = Field(default=None, alias="graphiti_mcp_url")
    mainBranch: str | None = Field(default=None, alias="main_branch")
    useClaudeMd: bool = Field(default=True, alias="use_claude_md")

    @field_validator("memoryBackend", mode="before")
    @classmethod
    def validate_memory_backend(cls, v):
        """Validate memoryBackend for backward compatibility."""
        if v is None:
            return "file"
        valid_backends = ["graphiti", "file"]
        if v not in valid_backends:
            # Fall back to file for invalid values (backward compatibility)
            return "file"
        return v

    @field_validator("notifications", mode="before")
    @classmethod
    def validate_notifications(cls, v):
        """Convert dict to NotificationSettings for backward compatibility."""
        if v is None:
            return NotificationSettings()
        if isinstance(v, dict):
            return NotificationSettings(**v)
        return v


class Project(ProjectBase):
    """Full project model with computed fields."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Unique project ID")
    name: str = Field(..., description="Display name")
    createdAt: str = Field(
        ..., alias="created_at", description="ISO timestamp when project was added"
    )
    updatedAt: str = Field(
        ...,
        alias="updated_at",
        description="ISO timestamp when project was last updated",
    )
    autoBuildPath: str | None = Field(
        None, alias="auto_build_path", description="Path to .magestic-ai if initialized"
    )
    settings: ProjectSettings = Field(default_factory=ProjectSettings)


# --------------------------------------------------------------------------
# Helper Functions
# --------------------------------------------------------------------------


def get_projects_file() -> Path:
    """Get path to the projects data file."""
    settings = get_settings()
    return Path(settings.PROJECTS_DATA_DIR) / "projects.json"


def load_projects() -> dict[str, dict]:
    """Load projects from disk."""
    projects_file = get_projects_file()
    if projects_file.exists():
        return json.loads(projects_file.read_text())
    return {}


def save_projects(projects: dict[str, dict]) -> None:
    """Save projects to disk."""
    projects_file = get_projects_file()
    projects_file.parent.mkdir(parents=True, exist_ok=True)
    projects_file.write_text(json.dumps(projects, indent=2))


def analyze_project(path: str) -> dict:
    """Analyze a project directory for git and Magestic AI status."""
    project_path = Path(path)

    # Check if it's a git repository
    is_git_repo = (project_path / ".git").exists()

    # Check for .magestic-ai directory
    magestic_ai_dir = project_path / ".magestic-ai"
    has_magestic_ai = magestic_ai_dir.exists()

    # Count specs/tasks
    task_count = 0
    specs_dir = magestic_ai_dir / "specs"
    if specs_dir.exists():
        task_count = len([d for d in specs_dir.iterdir() if d.is_dir()])

    return {
        "is_git_repo": is_git_repo,
        "has_magestic_ai": has_magestic_ai,
        "task_count": task_count,
    }


def project_to_response(project_id: str, project_data: dict) -> dict:
    """Convert stored project data to response dict matching frontend expectations."""
    analysis = analyze_project(project_data["path"])

    # Convert has_magestic_ai to autoBuildPath (string path or empty string)
    auto_build_path = ".magestic-ai" if analysis["has_magestic_ai"] else ""

    # Build settings: start with defaults, then overlay saved settings
    default_settings = {
        "model": "claude-sonnet-4-5-20250929",
        "memoryBackend": "file",
        "notifications": {
            "onTaskComplete": True,
            "onTaskFailed": True,
            "onReviewNeeded": True,
            "sound": True,
        },
        "graphitiMcpEnabled": False,
        "graphitiMcpUrl": None,
        "mainBranch": None,
        "useClaudeMd": True,
    }
    # Merge saved settings from projects.json (written by update_project_settings)
    saved_settings = project_data.get("settings", {})
    if saved_settings:
        # Merge notifications separately to preserve individual keys
        if "notifications" in saved_settings:
            default_settings["notifications"].update(saved_settings["notifications"])
            saved_settings = {
                k: v for k, v in saved_settings.items() if k != "notifications"
            }
        default_settings.update(saved_settings)

    return {
        "id": project_id,
        "path": project_data["path"],
        "name": project_data.get("name", Path(project_data["path"]).name),
        "createdAt": project_data.get("created_at", datetime.now().isoformat()),
        "updatedAt": project_data.get("updated_at", datetime.now().isoformat()),
        "autoBuildPath": auto_build_path,
        "settings": default_settings,
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("")
async def list_projects():
    """List all registered projects.

    Returns projects array directly (not wrapped) because
    the frontend api-client.ts adds the {success, data} wrapper automatically.
    """
    projects = load_projects()
    project_list = [project_to_response(pid, pdata) for pid, pdata in projects.items()]
    return project_list


class DiscoveredProject(BaseModel):
    """A discovered project folder."""

    name: str
    path: str
    has_git: bool = False
    has_package_json: bool = False
    has_requirements: bool = False
    has_magestic_ai: bool = False
    has_claude_md: bool = False


class ScanProjectsRequest(BaseModel):
    """Request model for scanning filesystem for projects."""

    basePath: str = Field(..., description="Base directory to scan for projects")
    maxDepth: int = Field(
        default=1, ge=1, le=5, description="Maximum scan depth (1-5, default 1)"
    )


@router.post("/scan")
async def scan_for_projects(request: ScanProjectsRequest):
    """
    Scan filesystem for Magestic AI projects.

    Recursively scans a directory tree to find potential project directories.
    Identifies projects by looking for indicators like:
    - .git directory (version control)
    - package.json (Node.js projects)
    - requirements.txt or pyproject.toml (Python projects)
    - .magestic-ai directory (Magestic AI initialized projects)
    - CLAUDE.md file (Claude project documentation)

    Args:
        request: ScanProjectsRequest with basePath and optional maxDepth

    Returns:
        List of DiscoveredProject objects with project metadata

    Raises:
        HTTPException: 400 if path doesn't exist or isn't a directory
    """
    try:
        # Validate and resolve base path
        base = Path(request.basePath).expanduser().resolve()

        if not base.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path does not exist: {request.basePath}",
            )

        if not base.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path is not a directory: {request.basePath}",
            )

        projects = []

        def scan_directory(dir_path: Path, current_depth: int):
            """Recursively scan directory for projects."""
            if current_depth > request.maxDepth:
                return

            try:
                # Sort entries for consistent ordering
                for entry in sorted(dir_path.iterdir(), key=lambda e: e.name.lower()):
                    if not entry.is_dir():
                        continue

                    # Skip hidden directories and common non-project dirs
                    if entry.name.startswith(".") or entry.name in (
                        "node_modules",
                        "__pycache__",
                        "venv",
                        ".venv",
                        "dist",
                        "build",
                        "target",
                        ".git",
                        "eggs",
                        ".eggs",
                        ".pytest_cache",
                        ".tox",
                        "htmlcov",
                        "coverage",
                    ):
                        continue

                    # Check for project indicators
                    has_git = (entry / ".git").exists()
                    has_package = (entry / "package.json").exists()
                    has_requirements = (entry / "requirements.txt").exists() or (
                        entry / "pyproject.toml"
                    ).exists()
                    has_magestic_ai = (entry / ".magestic-ai").exists()
                    has_claude_md = (entry / "CLAUDE.md").exists()

                    # If it looks like a project, add it
                    if has_git or has_package or has_requirements:
                        projects.append(
                            DiscoveredProject(
                                name=entry.name,
                                path=str(entry),
                                has_git=has_git,
                                has_package_json=has_package,
                                has_requirements=has_requirements,
                                has_magestic_ai=has_magestic_ai,
                                has_claude_md=has_claude_md,
                            )
                        )
                    elif current_depth < request.maxDepth:
                        # Not a project, but scan deeper if we haven't reached max depth
                        scan_directory(entry, current_depth + 1)

            except PermissionError:
                # Skip directories we can't read
                pass
            except Exception:
                # Skip directories that cause other errors (symlinks, etc)
                pass

        # Start scanning from base path at depth 1
        scan_directory(base, 1)

        # Return list directly - frontend api-client.ts adds {success, data} wrapper
        return [p.model_dump() for p in projects]

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        # Handle unexpected errors
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to scan for projects: {str(e)}",
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_project(project: ProjectCreate):
    """Add a new project (register a directory as an Magestic AI project).

    Returns project dict directly (not wrapped) because
    the frontend api-client.ts adds the {success, data} wrapper automatically.
    """
    # Validate path — create directory if it doesn't exist
    project_path = Path(project.path).expanduser()
    created_directory = False

    if not project_path.exists():
        try:
            project_path.mkdir(parents=True, exist_ok=True)
            created_directory = True
        except OSError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot create directory: {project.path} ({e})",
            )

    if not project_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path is not a directory: {project.path}",
        )

    # Check if already registered
    projects = load_projects()
    for pid, pdata in projects.items():
        if pdata["path"] == str(project_path.resolve()):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Project already registered",
            )

    # Create project entry
    project_id = str(uuid4())
    now = datetime.now().isoformat()
    project_data = {
        "path": str(project_path.resolve()),
        "name": project.name or project_path.name,
        "created_at": now,
        "updated_at": now,
    }

    projects[project_id] = project_data
    save_projects(projects)
    _kickoff_codegraph_index(project_data["path"])
    _kickoff_docs_generation(project_id, project_data["path"])

    response = project_to_response(project_id, project_data)
    if created_directory:
        response["createdDirectory"] = True
    return response


# --------------------------------------------------------------------------
# Clone from Git URL
# --------------------------------------------------------------------------

# Letters, digits, dot, underscore, hyphen — no slashes, no shell metachars,
# no leading dots. Used to guard against path traversal in the folder name.
_SAFE_FOLDER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")

# Default parent directory inside the container where cloned projects land.
# Bind-mounted to /home/saya/projects on the host (see .ops/compose.server.yml).
_DEFAULT_CLONE_PARENT = Path(
    os.environ.get("PROJECTS_CLONE_DIR", "/home/magesticai/projects")
)


class ProjectClone(BaseModel):
    """Model for cloning a remote git repository as a project."""

    url: str = Field(..., description="HTTPS git URL to clone")
    name: str | None = Field(
        None, description="Folder name (defaults to repo name from URL)"
    )
    branch: str | None = Field(
        None,
        description="Branch to check out (git clone --branch). Defaults to remote HEAD.",
    )
    target_dir: str | None = Field(
        None,
        description="Absolute parent directory for the clone (defaults to PROJECTS_CLONE_DIR)",
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL is required")
        if not v.startswith(("https://", "http://")):
            raise ValueError("Only https:// or http:// URLs are supported")
        return v


class RemoteBranchesRequest(BaseModel):
    """Body for listing a remote's branches before cloning."""

    url: str = Field(..., description="HTTPS git URL to inspect")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL is required")
        if not v.startswith(("https://", "http://")):
            raise ValueError("Only https:// or http:// URLs are supported")
        return v


@router.post("/git/remote-branches")
async def list_remote_branches(payload: RemoteBranchesRequest):
    """List branch names on a remote via ``git ls-remote --heads`` (pre-clone).

    Used to populate the clone dialog's branch dropdown. On any failure
    (auth/network/timeout) returns an empty list plus an ``error`` string so the
    UI can fall back to a free-text branch input rather than blocking the clone.
    """
    # GIT_TERMINAL_PROMPT=0 so a private repo fails fast instead of prompting.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            "--heads",
            "--",
            payload.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        return {"branches": [], "error": "Timed out listing remote branches."}
    except OSError as e:
        return {"branches": [], "error": str(e)}

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[:300]
        return {"branches": [], "error": detail or "Failed to list remote branches."}

    branches: list[dict] = []
    seen: set[str] = set()
    for line in stdout.decode("utf-8", "replace").splitlines():
        # Each line: "<sha>\trefs/heads/<branch>"
        parts = line.split("\trefs/heads/", 1)
        if len(parts) != 2:
            continue
        name = parts[1].strip()
        if name and name not in seen:
            seen.add(name)
            branches.append({"name": name})
    return {"branches": branches}


def _derive_repo_name(url: str) -> str:
    """Extract a default folder name from a git URL.

    'https://gitlab.com/group/sub/repo.git' -> 'repo'
    """
    name = url.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    name = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-.")
    return name or "project"


@router.post("/clone", status_code=status.HTTP_201_CREATED)
async def clone_project(payload: ProjectClone):
    """Clone a remote git repository into the projects directory and register it.

    Uses the container's pre-configured git credentials (~/.git-credentials +
    extraheader for self-hosted Bitbucket Server). Public repos work without
    any token configured.

    Returns the same shape as POST /projects so the frontend can reuse the
    post-add flow (init prompt, doc upload step).
    """
    folder_name = (payload.name or _derive_repo_name(payload.url)).strip()
    if not _SAFE_FOLDER_RE.match(folder_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Folder name may only contain letters, digits, '.', '_' and '-' "
            "(and may not start with a dot).",
        )

    parent = (
        Path(payload.target_dir).expanduser()
        if payload.target_dir
        else _DEFAULT_CLONE_PARENT
    )
    if not parent.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_dir must be an absolute path",
        )
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / folder_name

    if target.exists() and any(target.iterdir()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target path exists and is not empty: {target}",
        )

    # GIT_TERMINAL_PROMPT=0 makes git fail fast on auth instead of hanging.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    branch_args = (
        ["--branch", payload.branch.strip()]
        if payload.branch and payload.branch.strip()
        else []
    )
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        *branch_args,
        "--",
        payload.url,
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="git clone timed out after 10 minutes",
        )

    if proc.returncode != 0:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"git clone failed: {stderr.decode('utf-8', 'replace').strip()[:500]}",
        )

    resolved = str(target.resolve())
    projects = load_projects()
    # Idempotent: if this path is already registered, just return it.
    for pid, pdata in projects.items():
        if pdata["path"] == resolved:
            return project_to_response(pid, pdata)

    project_id = str(uuid4())
    now = datetime.now().isoformat()
    project_data = {
        "path": resolved,
        "name": payload.name or folder_name,
        "created_at": now,
        "updated_at": now,
    }
    projects[project_id] = project_data
    save_projects(projects)
    _kickoff_codegraph_index(resolved)
    _kickoff_docs_generation(project_id, resolved)

    response = project_to_response(project_id, project_data)
    response["createdDirectory"] = True
    return response


# --------------------------------------------------------------------------
# Clone multiple repos into one multi-repo project
# --------------------------------------------------------------------------


class RepoSpec(BaseModel):
    """One repository to clone inside a multi-repo project."""

    url: str = Field(..., description="HTTPS git URL to clone")
    name: str | None = Field(
        None, description="Child folder name (defaults to repo name from URL)"
    )
    branch: str | None = Field(
        None, description="Branch to check out for this repo (git clone --branch)."
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL is required")
        if not v.startswith(("https://", "http://")):
            raise ValueError("Only https:// or http:// URLs are supported")
        return v


class ProjectCloneMulti(BaseModel):
    """Clone several repos side-by-side into one composite project folder.

    The project folder itself is intentionally NOT a git repo: each repo is
    cloned into a child folder, so ``core.multi_repo.discover_repos`` sees a
    multi-repo project and the build machinery cuts one worktree per repo.
    """

    name: str = Field(..., description="Project folder name (the non-git parent)")
    repos: list[RepoSpec] = Field(
        ..., min_length=1, description="Repos to clone as children"
    )
    target_dir: str | None = Field(
        None,
        description="Absolute parent directory for the project (defaults to PROJECTS_CLONE_DIR)",
    )


async def _git_clone(
    url: str, target: Path, branch: str | None = None
) -> tuple[bool, str]:
    """Clone ``url`` into ``target``. Returns (ok, stderr).

    When ``branch`` is set, ``--branch <b>`` checks that branch out on clone;
    otherwise git uses the remote's default HEAD (unchanged behavior).
    """
    # GIT_TERMINAL_PROMPT=0 makes git fail fast on auth instead of hanging.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    branch_args = ["--branch", branch.strip()] if branch and branch.strip() else []
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        *branch_args,
        "--",
        url,
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "git clone timed out after 10 minutes"
    if proc.returncode != 0:
        return False, stderr.decode("utf-8", "replace").strip()[:500]
    return True, ""


@router.post("/clone-multi", status_code=status.HTTP_201_CREATED)
async def clone_multi_project(payload: ProjectCloneMulti):
    """Clone multiple repos into one multi-repo project and register it.

    Layout created (mirrors what ``discover_repos`` expects):

        <parent>/<name>/            <- project root (NOT a git repo)
        <parent>/<name>/<repo-a>/   <- clone of repo a
        <parent>/<name>/<repo-b>/   <- clone of repo b

    Cloning runs as the web-server process (the container's ``magesticai``
    user), so the worktree-ownership chown gotcha of host-side clones is
    avoided. Returns the same shape as POST /projects/clone.
    """
    project_name = payload.name.strip()
    if not _SAFE_FOLDER_RE.match(project_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project name may only contain letters, digits, '.', '_' and '-' "
            "(and may not start with a dot).",
        )

    # Resolve each repo's child folder name up front, rejecting bad/duplicate names.
    child_names: list[str] = []
    seen: set[str] = set()
    for spec in payload.repos:
        child = (spec.name or _derive_repo_name(spec.url)).strip()
        if not _SAFE_FOLDER_RE.match(child):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid repository folder name: {child!r}",
            )
        if child in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate repository folder name: {child!r}",
            )
        seen.add(child)
        child_names.append(child)

    parent = (
        Path(payload.target_dir).expanduser()
        if payload.target_dir
        else _DEFAULT_CLONE_PARENT
    )
    if not parent.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_dir must be an absolute path",
        )
    project_root = parent / project_name
    if project_root.exists() and any(project_root.iterdir()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target path exists and is not empty: {project_root}",
        )
    project_root.mkdir(parents=True, exist_ok=True)

    # Clone each repo; on any failure tear down the whole project folder so we
    # never register a half-cloned multi-repo project.
    for spec, child in zip(payload.repos, child_names):
        ok, err = await _git_clone(spec.url, project_root / child, spec.branch)
        if not ok:
            shutil.rmtree(project_root, ignore_errors=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"git clone failed for {child}: {err}",
            )

    resolved = str(project_root.resolve())
    projects = load_projects()
    for pid, pdata in projects.items():
        if pdata["path"] == resolved:
            return project_to_response(pid, pdata)

    project_id = str(uuid4())
    now = datetime.now().isoformat()
    project_data = {
        "path": resolved,
        "name": project_name,
        "created_at": now,
        "updated_at": now,
    }
    projects[project_id] = project_data
    save_projects(projects)
    _kickoff_codegraph_index(resolved)
    _kickoff_docs_generation(project_id, resolved)

    response = project_to_response(project_id, project_data)
    response["createdDirectory"] = True
    return response


# --------------------------------------------------------------------------
# Create from prompt
# --------------------------------------------------------------------------


_DEFAULT_GITIGNORE = (
    "# Auto-generated gitignore\n"
    "node_modules/\n"
    ".env\n"
    ".env.local\n"
    "__pycache__/\n"
    "*.pyc\n"
    ".venv/\n"
    "venv/\n"
    ".magestic-ai/\n"
    "dist/\n"
    "build/\n"
)


def _slugify_for_folder(text: str, fallback: str = "project") -> str:
    """Turn a free-form string into a safe folder name (matches _SAFE_FOLDER_RE)."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    words = re.findall(r"[A-Za-z0-9]+", first_line)[:6]
    slug = "-".join(w.lower() for w in words)
    slug = slug.strip("-.") or fallback
    if not slug[0].isalnum() and slug[0] != "_":
        slug = "p-" + slug
    return slug[:60]


class ProjectFromPrompt(BaseModel):
    """Model for creating a fresh project from a natural-language prompt."""

    prompt: str = Field(..., description="What the user wants built")
    name: str | None = Field(
        None, description="Folder name (defaults to slugified prompt)"
    )
    parent_dir: str | None = Field(
        None,
        description="Absolute parent directory (defaults to PROJECTS_CLONE_DIR)",
    )

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Prompt is required")
        if len(v) > 20_000:
            raise ValueError("Prompt is too long (max 20000 chars)")
        return v


@router.post("/from-prompt", status_code=status.HTTP_201_CREATED)
async def create_project_from_prompt(payload: ProjectFromPrompt):
    """Create a brand-new project directory from a natural-language prompt.

    Scaffolds an empty directory, runs `git init`, drops a `.gitignore` and a
    `README.md` containing the prompt, makes an initial commit, and registers
    the project. The frontend then opens the Task Creation Wizard pre-filled
    with the same prompt so agents can take over.
    """
    folder_name = (payload.name or _slugify_for_folder(payload.prompt)).strip()
    if not _SAFE_FOLDER_RE.match(folder_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Folder name may only contain letters, digits, '.', '_' and '-' "
            "(and may not start with a dot).",
        )

    parent = (
        Path(payload.parent_dir).expanduser()
        if payload.parent_dir
        else _DEFAULT_CLONE_PARENT
    )
    if not parent.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="parent_dir must be an absolute path",
        )
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / folder_name

    if target.exists() and any(target.iterdir()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target path exists and is not empty: {target}",
        )

    target.mkdir(parents=True, exist_ok=True)
    target_str = str(target)

    # git init + .gitignore + README + initial commit. README captures the
    # prompt so the agent has it as on-disk context even if the user navigates
    # away before launching the first task.
    init_result = run_git_command(["init"], target_str)
    if not init_result["success"]:
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"git init failed: {init_result.get('error')}",
        )

    (target / ".gitignore").write_text(_DEFAULT_GITIGNORE)
    (target / "README.md").write_text(
        f"# {folder_name}\n\n## Initial prompt\n\n{payload.prompt}\n"
    )

    run_git_command(["add", "-A"], target_str)
    run_git_command(
        ["commit", "-m", "Initial commit from prompt", "--allow-empty"],
        target_str,
    )

    resolved = str(target.resolve())
    projects = load_projects()
    for pid, pdata in projects.items():
        if pdata["path"] == resolved:
            response = project_to_response(pid, pdata)
            response["initialPrompt"] = payload.prompt
            return response

    project_id = str(uuid4())
    now = datetime.now().isoformat()
    project_data = {
        "path": resolved,
        "name": payload.name or folder_name,
        "created_at": now,
        "updated_at": now,
    }
    projects[project_id] = project_data
    save_projects(projects)

    response = project_to_response(project_id, project_data)
    response["createdDirectory"] = True
    response["initialPrompt"] = payload.prompt
    return response


@router.get("/{project_id}")
async def get_project(project_id: str):
    """Get a specific project by ID."""
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    return project_to_response(project_id, projects[project_id])


@router.put("/{project_id}")
async def update_project(project_id: str, project: ProjectCreate):
    """Update a project's metadata."""
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    # Update fields
    project_data = projects[project_id]
    if project.path:
        project_path = Path(project.path)
        if not project_path.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path does not exist: {project.path}",
            )
        project_data["path"] = str(project_path.resolve())

    if project.name:
        project_data["name"] = project.name

    project_data["updated_at"] = datetime.now().isoformat()

    projects[project_id] = project_data
    save_projects(projects)

    return project_to_response(project_id, project_data)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project(project_id: str):
    """Remove a project (unregister, does not delete files)."""
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    del projects[project_id]
    save_projects(projects)


@router.post("/{project_id}/initialize")
async def initialize_project(project_id: str):
    """Initialize Magestic AI in a project (create .magestic-ai directory).

    Returns InitializationResult format expected by frontend.
    """
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_data = projects[project_id]
    project_path = Path(project_data["path"])

    try:
        # Create .magestic-ai directory structure
        magestic_ai_dir = project_path / ".magestic-ai"
        (magestic_ai_dir / "specs").mkdir(parents=True, exist_ok=True)

        # Update timestamp and autoBuildPath
        project_data["updated_at"] = datetime.now().isoformat()
        project_data["autoBuildPath"] = ".magestic-ai"
        projects[project_id] = project_data
        save_projects(projects)

        # Return nested format expected by frontend
        return {"success": True, "data": {"success": True}}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/{project_id}/version")
async def check_project_version(project_id: str):
    """Check Magestic AI version info for a project."""
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_data = projects[project_id]
    project_path = Path(project_data["path"])
    magestic_ai_dir = project_path / ".magestic-ai"

    return {
        "success": True,
        "data": {"isInitialized": magestic_ai_dir.exists(), "updateAvailable": False},
    }


class NotificationSettingsUpdate(BaseModel):
    """Model for updating notification settings."""

    onTaskComplete: bool | None = None
    onTaskFailed: bool | None = None
    onReviewNeeded: bool | None = None
    sound: bool | None = None
    emailEnabled: bool | None = None


class ProjectSettingsUpdate(BaseModel):
    """Model for updating project settings.

    BUG-1.2-005: Added notifications field to allow updating notification preferences.
    BUG-1.2-003: Added memoryBackend validation.
    """

    model: str | None = None
    # BUG-1.2-003: Validate memoryBackend against allowed values
    memoryBackend: MemoryBackendType | None = None
    # BUG-1.2-005: Added notifications field so preferences can be updated via API
    notifications: NotificationSettingsUpdate | None = None
    graphitiMcpEnabled: bool | None = None
    graphitiMcpUrl: str | None = None
    mainBranch: str | None = None
    useClaudeMd: bool | None = None

    @field_validator("memoryBackend", mode="before")
    @classmethod
    def validate_memory_backend(cls, v):
        """Validate memoryBackend for backward compatibility."""
        if v is None:
            return None
        valid_backends = ["graphiti", "file"]
        if v not in valid_backends:
            # Return None for invalid values (won't update)
            return None
        return v


@router.patch("/{project_id}/settings")
async def update_project_settings(project_id: str, settings: ProjectSettingsUpdate):
    """Update project settings."""
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    try:
        project_data = projects[project_id]
        project_path = Path(project_data["path"])
        env_path = project_path / ".magestic-ai" / ".env"

        # Read existing .env or start fresh
        existing = {}
        if env_path.exists():
            for line in env_path.read_text().split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    existing[key.strip()] = value.strip()

        # Map ProjectSettingsUpdate fields to environment variables
        # Only update non-None values
        settings_dict = settings.model_dump(exclude_none=True)

        env_mapping = {
            "model": "MAGESTIC_AI_MODEL",
            "memoryBackend": "MEMORY_BACKEND",
            "graphitiMcpUrl": "GRAPHITI_MCP_URL",
            "mainBranch": "MAIN_BRANCH",
        }

        # Handle boolean settings with "true"/"false" string values
        bool_mapping = {
            "graphitiMcpEnabled": "GRAPHITI_ENABLED",
            "useClaudeMd": "USE_CLAUDE_MD",
        }

        # Update string/value settings
        for settings_key, env_key in env_mapping.items():
            if settings_key in settings_dict:
                existing[env_key] = str(settings_dict[settings_key])

        # Update boolean settings
        for settings_key, env_key in bool_mapping.items():
            if settings_key in settings_dict:
                existing[env_key] = "true" if settings_dict[settings_key] else "false"

        # Ensure .magestic-ai directory exists
        env_path.parent.mkdir(parents=True, exist_ok=True)

        # Write back to .env file
        content = "\n".join(f"{k}={v}" for k, v in existing.items())
        env_path.write_text(content)

        # Set secure file permissions (owner read/write only)
        env_path.chmod(0o600)

        # Also update settings in projects.json
        if "settings" not in project_data:
            project_data["settings"] = {}

        # BUG-1.2-005: Handle notifications field specially to merge with existing values
        if "notifications" in settings_dict:
            notifications_update = settings_dict.pop("notifications")
            if notifications_update:
                # Ensure notifications dict exists
                if "notifications" not in project_data["settings"]:
                    project_data["settings"]["notifications"] = {
                        "onTaskComplete": True,
                        "onTaskFailed": True,
                        "onReviewNeeded": True,
                        "sound": True,
                    }
                # Merge the update into existing notifications
                project_data["settings"]["notifications"].update(notifications_update)

        project_data["settings"].update(settings_dict)
        project_data["updated_at"] = datetime.now().isoformat()

        save_projects(projects)

        return {"success": True, "message": "Project settings updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update project settings: {str(e)}"
        )


@router.get("/{project_id}/worktrees")
async def list_project_worktrees(project_id: str, repo: str | None = Query(None)):
    """List worktrees for a project with detailed stats.

    For multi-repo projects, ``repo`` selects which child repo to inspect;
    it must be one of the repos detected for the project path.
    """
    import re
    import subprocess

    from ..services.git_repos import resolve_repo_cwd

    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_data = projects[project_id]
    git_cwd = resolve_repo_cwd(project_data["path"], repo)

    # Get the base branch (current branch of main repo)
    try:
        base_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=git_cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        base_branch = (
            base_result.stdout.strip() if base_result.returncode == 0 else "main"
        )
    except Exception:
        base_branch = "main"

    # List worktrees using git
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=git_cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"worktrees": []}

        # Parse git worktree list output
        raw_worktrees = []
        current = {}
        for line in result.stdout.split("\n"):
            if line.startswith("worktree "):
                if current:
                    raw_worktrees.append(current)
                current = {"path": line[9:]}
            elif line.startswith("branch "):
                current["branch"] = line[7:]
            elif line == "bare":
                current["bare"] = True
        if current:
            raw_worktrees.append(current)

        # Filter to only magestic-ai spec worktrees and enrich with stats
        enriched_worktrees = []
        for wt in raw_worktrees:
            wt_path = wt.get("path", "")
            branch = wt.get("branch", "")

            # Skip main worktree and bare repos
            if wt.get("bare") or wt_path == git_cwd:
                continue

            # Extract spec name from path (e.g., .magestic-ai/worktrees/tasks/001-feature)
            # Pattern: magestic-ai worktrees are in .magestic-ai/worktrees/tasks/{spec-name}
            spec_match = re.search(r"/\.magestic-ai/worktrees/tasks/([^/]+)$", wt_path)
            if not spec_match:
                continue

            spec_name = spec_match.group(1)

            # Get diff stats between base branch and worktree branch
            try:
                # Get commit count
                commit_result = subprocess.run(
                    ["git", "rev-list", "--count", f"{base_branch}..{branch}"],
                    cwd=wt_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                commit_count = (
                    int(commit_result.stdout.strip())
                    if commit_result.returncode == 0
                    else 0
                )

                # Get diff stats
                diff_result = subprocess.run(
                    ["git", "diff", "--shortstat", f"{base_branch}...{branch}"],
                    cwd=wt_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                files_changed = 0
                additions = 0
                deletions = 0

                if diff_result.returncode == 0 and diff_result.stdout.strip():
                    stat_line = diff_result.stdout.strip()
                    # Parse "X files changed, Y insertions(+), Z deletions(-)"
                    files_match = re.search(r"(\d+) files? changed", stat_line)
                    add_match = re.search(r"(\d+) insertions?\(\+\)", stat_line)
                    del_match = re.search(r"(\d+) deletions?\(-\)", stat_line)

                    files_changed = int(files_match.group(1)) if files_match else 0
                    additions = int(add_match.group(1)) if add_match else 0
                    deletions = int(del_match.group(1)) if del_match else 0

                enriched_worktrees.append(
                    {
                        "specName": spec_name,
                        "path": wt_path,
                        "branch": branch.replace("refs/heads/", ""),
                        "baseBranch": base_branch,
                        "commitCount": commit_count,
                        "filesChanged": files_changed,
                        "additions": additions,
                        "deletions": deletions,
                    }
                )
            except Exception:
                # Still include the worktree with default stats
                enriched_worktrees.append(
                    {
                        "specName": spec_name,
                        "path": wt_path,
                        "branch": branch.replace("refs/heads/", ""),
                        "baseBranch": base_branch,
                        "commitCount": 0,
                        "filesChanged": 0,
                        "additions": 0,
                        "deletions": 0,
                    }
                )

        return {"worktrees": enriched_worktrees}
    except Exception as e:
        return {"worktrees": [], "error": str(e)}


@router.get("/{project_id}/tasks")
async def list_project_tasks(project_id: str):
    """List all tasks for a specific project.

    Returns tasks array directly (not wrapped) because
    the frontend api-client.ts adds the {success, data} wrapper automatically.
    """
    # Import here to avoid circular import
    from . import tasks as tasks_module

    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    spec_dirs = tasks_module.get_spec_dirs(project_path)

    all_tasks = []
    for spec_dir in spec_dirs:
        task = tasks_module.spec_to_task(project_id, spec_dir)
        all_tasks.append(tasks_module.task_to_dict(task))

    # Sort by created_at descending
    all_tasks.sort(key=lambda t: t.get("createdAt", ""), reverse=True)

    return all_tasks


class TaskCreateRequest(BaseModel):
    """Request model for creating a task via project endpoint."""

    title: str = Field(
        default="", description="Task title (optional, auto-generated if empty)"
    )
    description: str = Field(
        ..., min_length=1, description="Task description (required)"
    )
    metadata: dict | None = Field(default=None, description="Optional task metadata")


@router.post("/{project_id}/tasks")
async def create_project_task(project_id: str, task_data: TaskCreateRequest):
    """Create a new task in a project.

    This endpoint delegates to the tasks module for actual creation.
    """
    import json
    from datetime import datetime

    from . import tasks as tasks_module

    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    # Auto-generate title from description if empty
    title = task_data.title.strip()
    if not title:
        # Generate title from first line/sentence of description
        desc_lines = task_data.description.strip().split("\n")
        first_line = desc_lines[0].strip()
        # Truncate to reasonable length
        title = first_line[:80] + ("..." if len(first_line) > 80 else "")
        if not title:
            title = "New Task"

    # Use the create_task logic
    project_path = Path(projects[project_id]["path"])

    # Ensure .magestic-ai/specs exists
    specs_dir = project_path / ".magestic-ai" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)

    # Generate spec ID and create directory
    spec_id = tasks_module.get_next_spec_id(project_path, title)
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir()

    # Create initial spec.md
    spec_content = f"""# {title}

{task_data.description}

## Acceptance Criteria

- [ ] Feature works as described
- [ ] Tests pass
- [ ] Code review approved
"""
    (spec_dir / "spec.md").write_text(spec_content)

    # Create requirements.json
    requirements = {
        "title": title,
        "description": task_data.description,
        "created_at": datetime.now().isoformat(),
    }
    if task_data.metadata:
        requirements["metadata"] = task_data.metadata
    (spec_dir / "requirements.json").write_text(json.dumps(requirements, indent=2))

    # Create task_metadata.json for phase_config.py to read model/thinking settings
    # This file is read by the backend to determine per-phase model and thinking levels
    if task_data.metadata:
        task_metadata = {}
        # Copy model-related fields that phase_config.py expects
        # Also include 'mode' for Quick Mode prompt selection and 'requireReviewBeforeCoding' for approval gate
        # Also include selectedSkills so agent_service.py can inject skill context
        # baseBranch/repoPath persisted here so the build honors the user's git
        # choices even though the "Start" request doesn't re-send them.
        model_fields = [
            "model",
            "thinkingLevel",
            "isAutoProfile",
            "phaseModels",
            "phaseThinking",
            "mode",
            "agentMode",
            "requireReviewBeforeCoding",
            "selectedSkills",
            "baseBranch",
            "repoPath",
            "repoPaths",
            "taskType",
            "bugReport",
            "uiCheck",
        ]
        for field in model_fields:
            if field in task_data.metadata:
                task_metadata[field] = task_data.metadata[field]

        # Validate the git targets against the project's actual repos so a task
        # can only build the project's parent folder or one of its child repos.
        from ..services.git_repos import resolve_git_repos

        repos = resolve_git_repos(str(project_path))
        allowed = {r["path"] for r in repos}

        repo_path = task_metadata.get("repoPath")
        if repo_path and repo_path not in allowed:
            task_metadata.pop("repoPath", None)

        # Multi-repo: keep only valid repoPaths; an explicit repoPaths list means
        # the task spans those repos. When the client sent neither a single
        # repoPath nor a list AND the project has several repos, default to ALL
        # of them so cross-cutting features (backend + frontend) build together.
        repo_paths = task_metadata.get("repoPaths")
        if isinstance(repo_paths, list):
            repo_paths = [p for p in repo_paths if p in allowed]
            if repo_paths:
                task_metadata["repoPaths"] = repo_paths
            else:
                task_metadata.pop("repoPaths", None)
        if (
            not task_metadata.get("repoPath")
            and not task_metadata.get("repoPaths")
            and len([r for r in repos if not r.get("isRoot")]) > 1
        ):
            task_metadata["repoPaths"] = [
                r["path"] for r in repos if not r.get("isRoot")
            ]

        if task_metadata:
            (spec_dir / "task_metadata.json").write_text(
                json.dumps(task_metadata, indent=2)
            )

    task = tasks_module.spec_to_task(project_id, spec_dir)
    return tasks_module.task_to_dict(task)


@router.post("/{project_id}/tasks/{spec_id}/logs/watch")
async def watch_project_task_logs(project_id: str, spec_id: str):
    """
    Start watching task logs (stub endpoint for frontend compatibility).

    Note: Log streaming is handled via WebSocket, this endpoint is a no-op
    that prevents 404 errors in the frontend.
    """
    return {"success": True, "message": "Log watching handled via WebSocket"}


@router.post("/{project_id}/tasks/{spec_id}/logs/unwatch")
async def unwatch_project_task_logs(project_id: str, spec_id: str):
    """
    Stop watching task logs (stub endpoint for frontend compatibility).

    Note: Log streaming is handled via WebSocket, this endpoint is a no-op
    that prevents 404 errors in the frontend.
    """
    return {"success": True, "message": "Log unwatching handled via WebSocket"}


@router.get("/{project_id}/tasks/{spec_id}/logs")
async def get_project_task_logs(project_id: str, spec_id: str):
    """Get logs for a task (delegates to tasks router)."""
    from . import tasks as tasks_module

    task_id = f"{project_id}:{spec_id}"
    return await tasks_module.get_task_logs(task_id)


@router.get("/{project_id}/tasks/{spec_id}/reproduction-report")
async def get_project_task_reproduction_report(project_id: str, spec_id: str):
    """Get the bug reproduction report for a task (delegates to tasks router)."""
    from . import tasks as tasks_module

    task_id = f"{project_id}:{spec_id}"
    return await tasks_module.get_reproduction_report(task_id)


@router.get("/{project_id}/tasks/{spec_id}/ui-check-report")
async def get_project_task_ui_check_report(project_id: str, spec_id: str):
    """Get the UI-check report for a task (delegates to tasks router)."""
    from . import tasks as tasks_module

    task_id = f"{project_id}:{spec_id}"
    return await tasks_module.get_ui_check_report(task_id)


# --------------------------------------------------------------------------
# Task Archive Routes
# --------------------------------------------------------------------------


class ArchiveTasksRequest(BaseModel):
    """Request to archive tasks."""

    taskIds: list[str] = Field(..., description="List of task IDs to archive")
    version: str | None = Field(
        None, description="Version tag for the archive (e.g., 'v1.2.0')"
    )


class UnarchiveTasksRequest(BaseModel):
    """Request to unarchive tasks."""

    taskIds: list[str] = Field(..., description="List of task IDs to unarchive")


@router.post("/{project_id}/tasks/archive")
async def archive_tasks(project_id: str, request: ArchiveTasksRequest):
    """Archive completed tasks.

    Adds archivedAt timestamp and optional version to task metadata.
    Archived tasks remain in their spec directories but are hidden from
    the default Kanban view.
    """
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    specs_dir = project_path / ".magestic-ai" / "specs"

    archived_count = 0
    errors = []

    for task_id in request.taskIds:
        # Task ID format is "project_id:spec_id"
        if ":" in task_id:
            _, spec_id = task_id.split(":", 1)
        else:
            spec_id = task_id

        spec_dir = specs_dir / spec_id
        if not spec_dir.exists():
            errors.append(f"Task {spec_id} not found")
            continue

        # Update implementation_plan.json with archive metadata
        plan_file = spec_dir / "implementation_plan.json"
        plan = {}
        if plan_file.exists():
            try:
                plan = json.loads(plan_file.read_text())
            except json.JSONDecodeError:
                plan = {}

        # Add archive metadata
        plan["archivedAt"] = datetime.now().isoformat()
        if request.version:
            plan["archivedInVersion"] = request.version

        plan_file.write_text(json.dumps(plan, indent=2))
        archived_count += 1

    if errors and archived_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(errors),
        )

    return {
        "success": True,
        "archivedCount": archived_count,
        "errors": errors if errors else None,
    }


@router.post("/{project_id}/tasks/unarchive")
async def unarchive_tasks(project_id: str, request: UnarchiveTasksRequest):
    """Unarchive tasks.

    Removes archivedAt and archivedInVersion from task metadata,
    making them visible in the Kanban board again.
    """
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    specs_dir = project_path / ".magestic-ai" / "specs"

    unarchived_count = 0
    errors = []

    for task_id in request.taskIds:
        # Task ID format is "project_id:spec_id"
        if ":" in task_id:
            _, spec_id = task_id.split(":", 1)
        else:
            spec_id = task_id

        spec_dir = specs_dir / spec_id
        if not spec_dir.exists():
            errors.append(f"Task {spec_id} not found")
            continue

        # Update implementation_plan.json to remove archive metadata
        plan_file = spec_dir / "implementation_plan.json"
        if not plan_file.exists():
            errors.append(f"Task {spec_id} has no plan file")
            continue

        try:
            plan = json.loads(plan_file.read_text())
        except json.JSONDecodeError:
            errors.append(f"Task {spec_id} has invalid plan file")
            continue

        # Remove archive metadata
        plan.pop("archivedAt", None)
        plan.pop("archivedInVersion", None)

        plan_file.write_text(json.dumps(plan, indent=2))
        unarchived_count += 1

    if errors and unarchived_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(errors),
        )

    return {
        "success": True,
        "unarchivedCount": unarchived_count,
        "errors": errors if errors else None,
    }
