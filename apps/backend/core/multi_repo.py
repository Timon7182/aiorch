#!/usr/bin/env python3
"""
Multi-Repo Composite Workspace
==============================

Some projects are a *parent folder* holding several independent git repos
(e.g. ``cts/`` with ``backend/`` and ``frontend/``). A single task often needs
to touch more than one of them — but git worktrees are per-repo, so the
single-repo machinery in :mod:`worktree` can only build one.

This module adds a **composite workspace**: for each selected repo it cuts a
worktree into a sub-folder of one shared task directory that mirrors the
project's real layout:

    .magestic-ai/worktrees/tasks/{spec}/      <- composite root (agent cwd)
    .magestic-ai/worktrees/tasks/{spec}/backend/   <- worktree of cts-backend
    .magestic-ai/worktrees/tasks/{spec}/frontend/  <- worktree of frontend
    .magestic-ai/worktrees/tasks/{spec}/.magestic-ai/specs/{spec}/  <- spec files

Because the composite root sits at the *same* path the single-repo worktree
used, the web UI's spec-file sync keeps working unchanged. The agent's cwd is
the composite root, so it sees ``backend/`` and ``frontend/`` exactly as it
would in the real project, and git operations inside each sub-folder act on
that repo's worktree.

All multi-repo logic lives here so the single-repo path in :mod:`worktree`
stays byte-for-byte identical — a multi-repo project is opt-in (>1 repo
discovered or explicit ``repoPaths``), everything else is untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from worktree import WorktreeInfo

logger = logging.getLogger(__name__)


class MultiRepoError(Exception):
    """Error during multi-repo workspace operations."""


def discover_repos(project_dir: Path) -> list[Path]:
    """Return the git repos backing a project, in stable order.

    Mirrors the web server's ``git_repos.resolve_git_repos``: if ``project_dir``
    is itself a repo, returns just it; otherwise every immediate child holding a
    ``.git`` entry (following symlinks, so ``backend -> ../cts-backend`` counts).
    """
    project_dir = Path(project_dir)
    if (project_dir / ".git").exists():
        return [project_dir]

    repos: list[Path] = []
    try:
        children = sorted(project_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return repos
    for child in children:
        try:
            if child.is_dir() and (child / ".git").exists():
                repos.append(child)
        except OSError:
            continue
    return repos


def relname_for(project_dir: Path, repo_path: Path) -> str:
    """The folder name a repo occupies under the project root.

    For ``cts/backend`` -> ``"backend"``. For a project that is itself the repo
    -> the repo's own folder name. This is the sub-folder used inside the
    composite root, so the layout mirrors the real project.
    """
    project_dir = Path(project_dir)
    repo_path = Path(repo_path)
    try:
        rel = repo_path.relative_to(project_dir)
        # Only the first path component matters (repos are immediate children).
        return rel.parts[0] if rel.parts else repo_path.name
    except ValueError:
        # repo_path isn't under project_dir (e.g. a symlink target resolved
        # elsewhere) — fall back to its own name.
        return repo_path.name


def _sanitize_branch_name(raw: str) -> str | None:
    """Coerce a user string into a valid git branch name, or ``None``.

    Kept identical to :meth:`worktree.WorktreeManager._sanitize_branch_name` so
    multi-repo and single-repo tasks honor ``customBranchName`` the same way.
    """
    name = raw.strip()
    if not name:
        return None
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[~^:?*\[\\\x00-\x1f\x7f]", "", name)
    name = re.sub(r"/{2,}", "/", name).strip("/.")
    if not name or ".." in name or "@{" in name or name.endswith(".lock"):
        return None
    if any(part.startswith(".") or part == "" for part in name.split("/")):
        return None
    return name


@dataclass
class RepoWorktree:
    """A single repo's worktree within the composite workspace."""

    repo_path: Path
    relname: str
    worktree_path: Path
    branch: str
    base_branch: str


@dataclass
class MultiRepoWorkspace:
    """Manages a composite, multi-repo worktree for one spec.

    Construct with the discovered repos, call :meth:`setup` to materialize the
    composite, then :meth:`changed_repos` / :meth:`merge` to inspect or land the
    work. The object intentionally mirrors the slice of :class:`WorktreeManager`
    that callers use (``.path``), so build/merge code can treat both uniformly.
    """

    project_dir: Path
    spec_name: str
    repo_paths: list[Path]
    base_branch: str | None = None
    worktrees_root: Path | None = None
    worktrees: list[RepoWorktree] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir)
        self.repo_paths = [Path(p) for p in self.repo_paths]
        root = Path(self.worktrees_root or self.project_dir)
        self.composite_root = (
            root / ".magestic-ai" / "worktrees" / "tasks" / self.spec_name
        )
        self.specs_dir = root / ".magestic-ai" / "specs"

    # ----- properties for WorktreeManager-compatible duck typing -----

    @property
    def path(self) -> Path:
        return self.composite_root

    def get_worktree_info(self, spec_name: str | None = None):
        """Return a WorktreeInfo-shaped object for the composite.

        ``finalize_workspace`` (the only consumer in the auto_continue/web path)
        reads ``.path``; ``.branch`` / ``.base_branch`` are filled from the first
        repo so any caller that inspects them gets sane values. Returns ``None``
        if the composite hasn't been set up.
        """
        if not self.composite_root.exists():
            return None
        first = self.worktrees[0] if self.worktrees else None
        return WorktreeInfo(
            path=self.composite_root,
            branch=first.branch if first else self._branch_name(),
            spec_name=self.spec_name,
            base_branch=first.base_branch if first else (self.base_branch or "main"),
            is_active=True,
        )

    # ----- git helpers -----

    @staticmethod
    def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        # safe.directory=* so git works on repos owned by a different uid than
        # the process (dockerized deploy: the web/agent runs as one uid while a
        # bind-mounted child repo like cts/frontend is owned by another). Without
        # it git aborts with "detected dubious ownership" and worktree add fails.
        return subprocess.run(
            ["git", "-c", "safe.directory=*", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _detect_base_branch(self, repo_path: Path) -> str:
        """Per-repo base branch: DEFAULT_BRANCH env, else main/master, else current.

        Detected per repo because two repos can legitimately differ (one on
        ``main``, one on ``master``).
        """
        if self.base_branch:
            chk = self._run_git(
                ["rev-parse", "--verify", self.base_branch], repo_path
            )
            if chk.returncode == 0:
                return self.base_branch
        env_branch = os.getenv("DEFAULT_BRANCH")
        if env_branch:
            chk = self._run_git(["rev-parse", "--verify", env_branch], repo_path)
            if chk.returncode == 0:
                return env_branch
        for branch in ("main", "master"):
            chk = self._run_git(["rev-parse", "--verify", branch], repo_path)
            if chk.returncode == 0:
                return branch
        cur = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
        return cur.stdout.strip() or "main"

    def _branch_name(self) -> str:
        """Branch used across all repos for this task (custom or feature/{spec})."""
        spec_dir = self.specs_dir / self.spec_name
        sources: list[tuple[Path, str | None]] = [
            (spec_dir / "task_metadata.json", None),
            (spec_dir / "requirements.json", "metadata"),
        ]
        for path, sub_key in sources:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if sub_key:
                data = data.get(sub_key, {}) if isinstance(data, dict) else {}
            raw = data.get("customBranchName") if isinstance(data, dict) else None
            if raw:
                sanitized = _sanitize_branch_name(str(raw))
                if sanitized:
                    return sanitized
        return f"feature/{self.spec_name}"

    # ----- lifecycle -----

    def setup(self) -> Path:
        """Create (or reuse) the composite worktree for every selected repo.

        Returns the composite root (the agent's working directory).
        """
        self.composite_root.mkdir(parents=True, exist_ok=True)
        branch_name = self._branch_name()
        self.worktrees = []

        for repo_path in self.repo_paths:
            relname = relname_for(self.project_dir, repo_path)
            wt_path = self.composite_root / relname
            base = self._detect_base_branch(repo_path)

            existing = self._existing_worktree(repo_path, wt_path)
            if existing:
                logger.info(
                    "[multi_repo] reusing worktree %s (branch %s)", wt_path, existing
                )
                self.worktrees.append(
                    RepoWorktree(repo_path, relname, wt_path, existing, base)
                )
                continue

            self._create_worktree(repo_path, wt_path, branch_name, base)
            self.worktrees.append(
                RepoWorktree(repo_path, relname, wt_path, branch_name, base)
            )

        return self.composite_root

    def _existing_worktree(self, repo_path: Path, wt_path: Path) -> str | None:
        """Return the branch of an already-valid worktree at ``wt_path``, else None."""
        git_file = wt_path / ".git"
        if not (git_file.exists() and git_file.is_file()):
            return None
        res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt_path)
        if res.returncode != 0:
            return None
        return res.stdout.strip() or None

    def _create_worktree(
        self, repo_path: Path, wt_path: Path, branch_name: str, base: str
    ) -> None:
        # Clear any stale dir/registration so `worktree add` won't refuse.
        if wt_path.exists():
            git_file = wt_path / ".git"
            if git_file.exists() and git_file.is_file():
                self._run_git(
                    ["worktree", "remove", "--force", str(wt_path)], repo_path
                )
            else:
                shutil.rmtree(wt_path, ignore_errors=True)
        self._run_git(["worktree", "prune"], repo_path)
        # Drop a leftover branch from a crashed run so -b doesn't collide.
        self._run_git(["branch", "-D", branch_name], repo_path)

        res = self._run_git(
            ["worktree", "add", "-b", branch_name, str(wt_path), base], repo_path
        )
        if res.returncode != 0:
            raise MultiRepoError(
                f"Failed to create worktree for {repo_path.name} "
                f"({branch_name} from {base}): {res.stderr.strip()}"
            )
        logger.info(
            "[multi_repo] created worktree %s on %s from %s",
            wt_path,
            branch_name,
            base,
        )

    # ----- inspection -----

    def changed_repos(self) -> dict[str, bool]:
        """Map ``relname -> did this repo gain commits/changes on the task branch``.

        Drives change-aware deploy: only repos that actually changed need to be
        rebuilt. A repo with zero commits ahead of its base is reported False.
        """
        changed: dict[str, bool] = {}
        for wt in self.worktrees:
            count = self._run_git(
                ["rev-list", "--count", f"{wt.base_branch}..HEAD"], wt.worktree_path
            )
            n = 0
            if count.returncode == 0:
                try:
                    n = int(count.stdout.strip() or "0")
                except ValueError:
                    n = 0
            if n == 0:
                # No commits — still flag if there are uncommitted edits.
                dirty = self._run_git(["status", "--porcelain"], wt.worktree_path)
                changed[wt.relname] = bool(dirty.stdout.strip())
            else:
                changed[wt.relname] = True
        return changed

    # ----- merge -----

    def merge(self, no_commit: bool = False) -> dict[str, bool]:
        """Merge each repo's task branch back into that repo's base branch.

        Returns ``relname -> merged_ok``. Repos with no changes are skipped
        (reported True). Unlike the single-repo path, each repo is merged
        independently so one clean repo doesn't block another.
        """
        results: dict[str, bool] = {}
        for wt in self.worktrees:
            results[wt.relname] = self._merge_one(wt, no_commit=no_commit)
        return results

    def _merge_one(self, wt: RepoWorktree, no_commit: bool) -> bool:
        # Nothing to merge if the branch has no commits ahead of base.
        count = self._run_git(
            ["rev-list", "--count", f"{wt.base_branch}..{wt.branch}"], wt.repo_path
        )
        if count.returncode == 0 and (count.stdout.strip() or "0") == "0":
            logger.info("[multi_repo] %s: nothing to merge", wt.relname)
            return True

        co = self._run_git(["checkout", wt.base_branch], wt.repo_path)
        if co.returncode != 0:
            logger.error(
                "[multi_repo] %s: cannot checkout %s: %s",
                wt.relname,
                wt.base_branch,
                co.stderr.strip(),
            )
            return False

        merge_args = ["merge", "--no-ff", wt.branch]
        if no_commit:
            merge_args.append("--no-commit")
        else:
            merge_args.extend(["-m", f"Merge {wt.branch}"])
        res = self._run_git(merge_args, wt.repo_path)
        if res.returncode != 0:
            logger.error(
                "[multi_repo] %s: merge conflict, aborting: %s",
                wt.relname,
                res.stderr.strip(),
            )
            self._run_git(["merge", "--abort"], wt.repo_path)
            return False

        if no_commit:
            self._unstage_internal(wt.repo_path)
        logger.info("[multi_repo] %s: merged %s into %s", wt.relname, wt.branch, wt.base_branch)
        return True

    def _unstage_internal(self, repo_path: Path) -> None:
        """Unstage .magestic-ai/runtime files staged by a --no-commit merge."""
        res = self._run_git(["diff", "--cached", "--name-only"], repo_path)
        if res.returncode != 0 or not res.stdout.strip():
            return
        root_files = {".magestic-ai-security.json", ".magestic-ai-status"}
        for file in res.stdout.strip().split("\n"):
            f = file.strip()
            if not f:
                continue
            if f in root_files or f.startswith(".magestic-ai/") or "/.magestic-ai/" in f:
                self._run_git(["reset", "HEAD", "--", f], repo_path)


def should_use_multi_repo(
    repo_paths: list[Path] | None, discovered: list[Path]
) -> bool:
    """Decide whether a build should use the composite multi-repo path.

    True when the task explicitly targets >1 repo, or (no explicit selection)
    the project resolves to more than one repo. A single repo always uses the
    original single-repo worktree path, so existing projects are unaffected.
    """
    if repo_paths and len(repo_paths) > 1:
        return True
    if not repo_paths and len(discovered) > 1:
        return True
    return False
