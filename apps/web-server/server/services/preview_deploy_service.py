"""Preview-deploy orchestrator.

Builds a finished task's worktree into an isolated Docker stack on a target host
(via the allowlisted preview-runner.sh over SSH), returns a live URL/IP, and
manages the lifecycle (concurrency cap + TTL teardown). Also handles promotion of
a validated preview onto one of the two long-lived static lanes.

State lives in two places:
  - per-task: <spec_dir>/task_metadata.json -> "preview" key (what the UI reads)
  - the host: ~/.magestic-preview/preview-<slug>/ (compose + meta, owned by the runner)

The long-running build/deploy runs in a background thread (paramiko is blocking);
the UI polls GET /tasks/{id}/deploy-preview for status.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import get_data_file
from . import deploy_config as dc
from . import ext_storage, ssh_service

# Deploy script key in ServerProfile.deploys (must point at preview-runner.sh)
RUNNER_KEY = "preview"


class PreviewError(RuntimeError):
    pass


@dataclass
class TaskRef:
    project_id: str
    spec_id: str
    project_path: Path
    spec_dir: Path
    repo_path: Path
    worktree_path: Path
    slug: str


# ---------------------------------------------------------------------------
# Task / project resolution (mirrors create_pr_from_task in routes/tasks.py)
# ---------------------------------------------------------------------------
def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s or "task")[:48].strip("-")


def resolve_task(task_id: str) -> TaskRef:
    if ":" not in task_id:
        raise PreviewError("Task ID must include project ID (format: project_id:spec_id)")
    project_id, spec_id = task_id.split(":", 1)

    projects_file = get_data_file("projects.json")
    if not projects_file.exists():
        raise PreviewError("Projects file not found")
    projects_data = json.loads(projects_file.read_text())

    if isinstance(projects_data, dict):
        project = projects_data.get(project_id)
    else:
        project = next((p for p in projects_data if isinstance(p, dict) and p.get("id") == project_id), None)
    if not project:
        raise PreviewError(f"Project not found: {project_id}")
    project_path = Path(project["path"])

    spec_dir = project_path / ".magestic-ai" / "specs" / spec_id
    if not spec_dir.exists():
        raise PreviewError(f"Task {task_id} not found")

    repo_path = project_path
    meta_file = spec_dir / "task_metadata.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            rp = meta.get("repoPath")
            if rp and (Path(rp) / ".git").exists():
                repo_path = Path(rp)
        except (json.JSONDecodeError, OSError):
            pass

    # Worktrees usually live under the project root, but multi-repo projects
    # (e.g. cts → repoPath=cts-backend) keep them under the repo. Prefer the
    # project-root path, fall back to the repo's.
    worktree_path = project_path / ".magestic-ai" / "worktrees" / "tasks" / spec_id
    if not worktree_path.exists():
        repo_worktree = repo_path / ".magestic-ai" / "worktrees" / "tasks" / spec_id
        if repo_worktree.exists():
            worktree_path = repo_worktree

    return TaskRef(
        project_id=project_id,
        spec_id=spec_id,
        project_path=project_path,
        spec_dir=spec_dir,
        repo_path=repo_path,
        worktree_path=worktree_path,
        slug=_slugify(spec_id),
    )


def _git(args: list[str], cwd: Path) -> str | None:
    # safe.directory=* — bind-mounted child repos can be owned by a different uid
    # than this process; without it git aborts with "dubious ownership".
    try:
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", *args],
            cwd=str(cwd), capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _composite_worktrees(worktree_path: Path) -> list[dict[str, Any]]:
    """For a composite task dir, return per-repo sub-worktrees with change flags.

    A composite task dir is a plain folder whose children are git worktrees
    (``backend/``, ``frontend/``). Returns ``[{name, path, changed}]`` where
    ``changed`` means the repo gained commits (or has uncommitted edits) on the
    task branch vs its base. Returns ``[]`` for a classic single worktree (the
    task dir is itself a git worktree).
    """
    if (worktree_path / ".git").exists():
        return []  # classic single worktree
    out: list[dict[str, Any]] = []
    try:
        children = sorted(worktree_path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for child in children:
        try:
            if not (child.is_dir() and (child / ".git").exists()):
                continue
        except OSError:
            continue
        # Base = ref checked out in the repo's main worktree (first list entry).
        base = None
        wl = _git(["worktree", "list", "--porcelain"], child)
        if wl:
            for ln in wl.split("\n\n")[0].splitlines():
                if ln.startswith("branch "):
                    base = ln.split(" ", 1)[1].strip().replace("refs/heads/", "")
                    break
        changed = True
        if base:
            cnt = _git(["rev-list", "--count", f"{base}..HEAD"], child)
            if cnt is not None and cnt == "0":
                dirty = _git(["status", "--porcelain"], child)
                changed = bool(dirty)
        out.append({"name": child.name, "path": child, "changed": changed})
    return out


def _branch_of(path: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], path) or ""


def _short_sha(path: Path) -> str:
    return _git(["rev-parse", "--short", "HEAD"], path) or ""


# ---------------------------------------------------------------------------
# Target host (ServerProfile) lookup + container->host path translation
# ---------------------------------------------------------------------------
def find_target(target: str) -> dict[str, Any]:
    """Find a ServerProfile by id or name. `target` comes from deploy.config.json."""
    servers = ext_storage.load("servers")
    for s in servers:
        if s.get("id") == target or s.get("name") == target:
            return s
    raise PreviewError(
        f"deploy target {target!r} not found. Register a server (Settings > Servers) "
        f"named {target!r} with deploys.preview pointing at preview-runner.sh."
    )


def _to_host_path(path: Path, profile: dict[str, Any]) -> str:
    """Translate a container-visible path to the host path the runner will see.

    ServerProfile may carry host_path_map: {"/home/magesticai/projects": "/home/saya/projects"}.
    """
    p = str(path)
    mapping = profile.get("host_path_map") or {}
    for container_prefix, host_prefix in mapping.items():
        if p.startswith(container_prefix):
            return host_prefix + p[len(container_prefix):]
    return p


# ---------------------------------------------------------------------------
# Per-task preview state (task_metadata.json "preview" key)
# ---------------------------------------------------------------------------
def _read_meta(spec_dir: Path) -> dict[str, Any]:
    f = spec_dir / "task_metadata.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_preview_state(spec_dir: Path, patch: dict[str, Any]) -> dict[str, Any]:
    f = spec_dir / "task_metadata.json"
    meta = _read_meta(spec_dir)
    preview = dict(meta.get("preview") or {})
    preview.update(patch)
    preview["updatedAt"] = int(time.time())
    meta["preview"] = preview
    f.write_text(json.dumps(meta, indent=2))
    return preview


def get_preview(task_id: str) -> dict[str, Any]:
    ref = resolve_task(task_id)
    return (_read_meta(ref.spec_dir).get("preview")) or {"status": "none"}


# ---------------------------------------------------------------------------
# Runner invocation + JSON parsing
# ---------------------------------------------------------------------------
def _parse_runner_json(stdout: str) -> dict[str, Any]:
    """The runner prints logs to stderr and a single JSON object as the last
    non-empty stdout line."""
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise PreviewError("runner produced no parseable JSON result")


def _run_runner(profile: dict[str, Any], args: list[str], *, timeout: int = 1800) -> dict[str, Any]:
    res = ssh_service.run_script(profile, RUNNER_KEY, args, timeout=timeout)
    if res.exit_code != 0:
        # Try to surface the runner's JSON error, else the stderr tail.
        try:
            data = _parse_runner_json(res.stdout)
            raise PreviewError(data.get("error") or f"runner exited {res.exit_code}")
        except PreviewError:
            raise
        except Exception:
            tail = (res.stderr or res.stdout).strip()[-600:]
            raise PreviewError(f"runner exited {res.exit_code}: {tail}")
    return _parse_runner_json(res.stdout)


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------
def deploy_preview(task_id: str, *, lane_override: str | None = None) -> dict[str, Any]:
    """Validate inputs, set state to 'building', and run the deploy in the
    background. Returns the initial state immediately."""
    ref = resolve_task(task_id)
    if not ref.worktree_path.exists():
        raise PreviewError("No worktree found for this task")

    config = dc.load_deploy_config(ref.project_path)
    errors = dc.validate_config(config)
    if errors:
        raise PreviewError("invalid deploy config: " + "; ".join(errors))

    profile = find_target(config.get("target", ""))
    if not (profile.get("deploys") or {}).get(RUNNER_KEY):
        raise PreviewError(
            f"server {profile.get('name')!r} has no deploys.{RUNNER_KEY} script (preview-runner.sh)"
        )

    # Composite (multi-repo) task? Children of the task dir are per-repo
    # worktrees. For change-aware deploy we read git from one of them.
    composite = _composite_worktrees(ref.worktree_path)
    branch_probe = composite[0]["path"] if composite else ref.worktree_path

    branch = _branch_of(branch_probe)
    lane = (lane_override or dc.lane_for_branch(config, branch)).upper()
    if lane not in ("A", "B"):
        raise PreviewError(f"invalid lane: {lane}")
    sha = _short_sha(branch_probe)

    # Persist the fully-merged config next to the spec so the host (bind-mounted)
    # can read it; pass its host path to the runner.
    resolved_cfg = ref.spec_dir / "preview.deploy.config.json"
    resolved_cfg.write_text(json.dumps(config, indent=2))

    host_src = _to_host_path(ref.worktree_path, profile)
    host_cfg = _to_host_path(resolved_cfg, profile)

    # Per-repo source paths + change flags drive the change-aware cts recipe:
    # the runner rebuilds only the half (backend / frontend) that actually
    # changed and reuses the static lane's artifact for the other.
    repo_args: list[str] = []
    if composite:
        changed_names = [r["name"] for r in composite if r["changed"]]
        for r in composite:
            host_repo_src = _to_host_path(r["path"], profile)
            flag = "true" if r["changed"] else "false"
            if r["name"] == "backend":
                repo_args += ["--backend-src", host_repo_src, "--backend-changed", flag]
            elif r["name"] == "frontend":
                repo_args += ["--frontend-src", host_repo_src, "--frontend-changed", flag]
        _write_preview_state(ref.spec_dir, {"changedRepos": changed_names})

    state = _write_preview_state(ref.spec_dir, {
        "status": "building",
        "lane": lane,
        "branch": branch,
        "ref": sha,
        "url": None,
        "ip": None,
        "port": None,
        "db": None,
        "error": None,
        "startedAt": int(time.time()),
    })

    args = [
        "deploy",
        "--task", ref.slug,
        "--lane", lane,
        "--src", host_src,
        "--config", host_cfg,
    ]
    args += repo_args
    if sha:
        args += ["--ref", sha]

    def _worker() -> None:
        try:
            _write_preview_state(ref.spec_dir, {"status": "deploying"})
            data = _run_runner(profile, args, timeout=2400)
            _write_preview_state(ref.spec_dir, {
                "status": "running",
                "url": data.get("url"),
                "ip": data.get("ip"),
                "port": data.get("port"),
                "db": data.get("db"),
                "error": None,
            })
        except Exception as exc:  # noqa: BLE001 — record failure for the UI
            _write_preview_state(ref.spec_dir, {"status": "failed", "error": str(exc)})

    threading.Thread(target=_worker, name=f"preview-deploy-{ref.slug}", daemon=True).start()
    return state


def stop_preview(task_id: str) -> dict[str, Any]:
    ref = resolve_task(task_id)
    config = dc.load_deploy_config(ref.project_path)
    profile = find_target(config.get("target", ""))
    try:
        _run_runner(profile, ["teardown", "--task", ref.slug], timeout=300)
    finally:
        state = _write_preview_state(ref.spec_dir, {
            "status": "stopped", "url": None, "ip": None, "port": None, "db": None,
        })
    return state


def promote(task_id: str, *, lane_override: str | None = None) -> dict[str, Any]:
    ref = resolve_task(task_id)
    config = dc.load_deploy_config(ref.project_path)
    profile = find_target(config.get("target", ""))

    preview = (_read_meta(ref.spec_dir).get("preview")) or {}
    branch = preview.get("branch") or _branch_of(ref.worktree_path)
    lane = (lane_override or preview.get("lane") or dc.lane_for_branch(config, branch)).upper()
    if lane not in ("A", "B"):
        raise PreviewError(f"invalid lane: {lane}")

    _write_preview_state(ref.spec_dir, {"status": "promoting", "lane": lane})
    data = _run_runner(profile, ["promote", "--task", ref.slug, "--lane", lane], timeout=1200)
    # promote tears the preview down; reflect the static target in state.
    return _write_preview_state(ref.spec_dir, {
        "status": "promoted",
        "lane": lane,
        "staticUrl": data.get("url"),
        "port": data.get("port"),
        "db": data.get("db"),
        "url": None, "ip": None,
    })


def reap(target: str, ttl_hours: int) -> dict[str, Any]:
    """Tear down previews older than ttl_hours on the given target host."""
    profile = find_target(target)
    return _run_runner(profile, ["reap", "--ttl-hours", str(int(ttl_hours))], timeout=600)
