#!/usr/bin/env python3
"""
Git Worktree Manager - Per-Spec Architecture
=============================================

Each spec gets its own worktree:
- Worktree path: .magestic-ai/worktrees/tasks/{spec-name}/
- Branch name: feature/{spec-name}

This allows:
1. Multiple specs to be worked on simultaneously
2. Each spec's changes are isolated
3. Branches persist until explicitly merged
4. Clear 1:1:1 mapping: spec → worktree → branch
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def fetch_base_start_point(repo_dir: Path, base_branch: str) -> str:
    """Pull the latest remote state of ``base_branch`` into the local ref, then
    return ``base_branch`` (the start point a new worktree is cut from).

    So a new task starts from the latest pushed state of its base branch (main,
    or whatever branch the task selected) instead of a possibly-stale local
    checkout — and the local base stays a single consistent ref, so change
    detection (``base..HEAD``) and merge-back remain correct.

    How it "pulls" without disturbing work:
      * If the base branch is the one checked out in the repo's main worktree,
        fast-forward it (``merge --ff-only``) — only when the tree is clean, so
        local edits are never touched. A non-fast-forward (diverged) base is left
        alone.
      * Otherwise the base isn't checked out here, so its ref is moved straight
        to the remote tip (only when that's a fast-forward).

    Best-effort by design: no remote, fetch failure, missing credentials, a dirty
    tree, or a diverged base all fall back to the existing local base so a task
    is never blocked. ``GIT_TERMINAL_PROMPT=0`` + a timeout make a private repo
    without cached credentials fail fast instead of hanging on a prompt. Set
    ``MAGESTIC_FETCH_BEFORE_BUILD=0`` to skip the fetch entirely.
    """
    if os.getenv("MAGESTIC_FETCH_BEFORE_BUILD", "1").lower() in ("0", "false", "no"):
        return base_branch

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def _git(args: list[str]):
        try:
            return subprocess.run(
                ["git", "-c", "safe.directory=*", *args],
                cwd=str(repo_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env, timeout=180,
            )
        except subprocess.TimeoutExpired:
            return None

    # Pick a remote: prefer origin, else the first configured one.
    remote = "origin"
    got = _git(["remote", "get-url", "origin"])
    if got is None or got.returncode != 0:
        remotes_res = _git(["remote"])
        remotes = remotes_res.stdout.split() if remotes_res else []
        if not remotes:
            return base_branch  # no remote — local-only repo
        remote = remotes[0]

    fetched = _git(["fetch", remote, base_branch])
    if fetched is None or fetched.returncode != 0:
        detail = (fetched.stderr or "").strip()[:200] if fetched else "timeout"
        logger.info(
            "[worktree] fetch %s %s failed (%s); using local base",
            remote, base_branch, detail,
        )
        return base_branch

    tracking = f"{remote}/{base_branch}"
    if not _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/{tracking}"]) \
            or _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/{tracking}"]).returncode != 0:
        return base_branch  # remote doesn't have this branch

    current = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    current_branch = current.stdout.strip() if current else ""

    if current_branch == base_branch:
        # Base is checked out here: fast-forward it, but only on a clean tree.
        dirty = _git(["status", "--porcelain"])
        if dirty is not None and not dirty.stdout.strip():
            ff = _git(["merge", "--ff-only", tracking])
            if ff and ff.returncode == 0:
                logger.info("[worktree] fast-forwarded %s to %s", base_branch, tracking)
            else:
                logger.info("[worktree] %s not fast-forwardable; using local base", base_branch)
        else:
            logger.info("[worktree] %s tree dirty; not pulling, using local base", base_branch)
    else:
        # Base not checked out here — move its ref to the remote tip if that's a
        # fast-forward (never rewrite a diverged local base).
        anc = _git(["merge-base", "--is-ancestor", base_branch, tracking])
        if anc is not None and anc.returncode == 0:
            _git(["update-ref", f"refs/heads/{base_branch}", tracking])
            logger.info("[worktree] advanced ref %s to %s", base_branch, tracking)

    return base_branch


class WorktreeError(Exception):
    """Error during worktree operations."""

    pass


@dataclass
class WorktreeInfo:
    """Information about a spec's worktree."""

    path: Path
    branch: str
    spec_name: str
    base_branch: str
    is_active: bool = True
    commit_count: int = 0
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0


class WorktreeManager:
    """
    Manages per-spec Git worktrees.

    Each spec gets its own worktree in .magestic-ai/worktrees/tasks/{spec-name}/ with
    a corresponding branch feature/{spec-name}.
    """

    def __init__(
        self,
        project_dir: Path,
        base_branch: str | None = None,
        worktrees_root: Path | None = None,
    ):
        # ``project_dir`` is the git repository worktrees are cut from. For
        # multi-repo projects (a parent folder holding several repos) this is
        # the chosen child repo, while ``worktrees_root`` stays the project
        # root — so worktrees, and the web UI's file sync that reads them, keep
        # their usual location under the project regardless of which repo is
        # being built. When ``worktrees_root`` is omitted (the common
        # single-repo case) it defaults to ``project_dir``, preserving the
        # original layout exactly.
        self.project_dir = project_dir
        self.base_branch = base_branch or self._detect_base_branch()
        data_root = (worktrees_root or project_dir) / ".magestic-ai"
        self.worktrees_dir = data_root / "worktrees" / "tasks"
        # Spec metadata lives alongside the worktrees; we read it to honor a
        # user-supplied custom branch name set at task creation time.
        self.specs_dir = data_root / "specs"
        self._merge_lock = asyncio.Lock()

    def _detect_base_branch(self) -> str:
        """
        Detect the base branch for worktree creation.

        Priority order:
        1. DEFAULT_BRANCH environment variable
        2. Auto-detect main/master (if they exist)
        3. Fall back to current branch (with warning)

        Returns:
            The detected base branch name
        """
        # 1. Check for DEFAULT_BRANCH env var
        env_branch = os.getenv("DEFAULT_BRANCH")
        if env_branch:
            # Verify the branch exists
            result = subprocess.run(
                ["git", "rev-parse", "--verify", env_branch],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return env_branch
            else:
                print(
                    f"Warning: DEFAULT_BRANCH '{env_branch}' not found, auto-detecting..."
                )
                logger.warning(f"DEFAULT_BRANCH '{env_branch}' not found, auto-detecting base branch")

        # 2. Auto-detect main/master
        for branch in ["main", "master"]:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                return branch

        # 3. Fall back to current branch with warning
        current = self._get_current_branch()
        print("Warning: Could not find 'main' or 'master' branch.")
        print(f"Warning: Using current branch '{current}' as base for worktree.")
        print("Tip: Set DEFAULT_BRANCH=your-branch in .env to avoid this.")
        logger.warning(f"Could not find 'main' or 'master' branch. Using current branch '{current}' as base for worktree.", extra={
            "project_dir": str(self.project_dir),
            "current_branch": current,
        })
        return current

    def _get_current_branch(self) -> str:
        """Get the current git branch."""
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise WorktreeError(f"Failed to get current branch: {result.stderr}")
        return result.stdout.strip()

    def _run_git(
        self, args: list[str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        """Run a git command and return the result."""
        return subprocess.run(
            ["git"] + args,
            cwd=cwd or self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _unstage_gitignored_files(self) -> None:
        """
        Unstage any staged files that are gitignored in the current branch,
        plus any files in the .magestic-ai directory which should never be merged.

        This is needed after a --no-commit merge because files that exist in the
        source branch (like spec files in .magestic-ai/specs/) get staged even if
        they're gitignored in the target branch.
        """
        # Get list of staged files
        result = self._run_git(["diff", "--cached", "--name-only"])
        if result.returncode != 0 or not result.stdout.strip():
            return

        staged_files = result.stdout.strip().split("\n")

        # Files to unstage: gitignored files + .magestic-ai directory files
        files_to_unstage = set()

        # 1. Check which staged files are gitignored
        # git check-ignore returns the files that ARE ignored
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=self.project_dir,
            input="\n".join(staged_files),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.stdout.strip():
            for file in result.stdout.strip().split("\n"):
                if file.strip():
                    files_to_unstage.add(file.strip())

        # 2. Always unstage .magestic-ai directory files - these are project-specific
        # and should never be merged from the worktree branch
        magestic_ai_patterns = [".magestic-ai/", "magestic-ai/specs/"]
        # Root-level runtime files that agents may commit despite .gitignore
        magestic_ai_root_files = {".magestic-ai-security.json", ".magestic-ai-status"}
        for file in staged_files:
            file = file.strip()
            if not file:
                continue
            if file in magestic_ai_root_files:
                files_to_unstage.add(file)
                continue
            for pattern in magestic_ai_patterns:
                if file.startswith(pattern) or f"/{pattern}" in file:
                    files_to_unstage.add(file)
                    break

        if files_to_unstage:
            print(
                f"Unstaging {len(files_to_unstage)} magestic-ai/gitignored file(s)..."
            )
            logger.info(f"Unstaging {len(files_to_unstage)} magestic-ai/gitignored files", extra={
                "files": list(files_to_unstage)[:10],  # Log first 10 files
                "total_count": len(files_to_unstage),
            })
            # Unstage each file
            for file in files_to_unstage:
                self._run_git(["reset", "HEAD", "--", file])

    def setup(self) -> None:
        """Create worktrees directory if needed."""
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    # ==================== Per-Spec Worktree Methods ====================

    def get_worktree_path(self, spec_name: str) -> Path:
        """Get the worktree path for a spec."""
        return self.worktrees_dir / spec_name

    def get_branch_name(self, spec_name: str) -> str:
        """Get the branch name for a spec.

        Honors a custom branch name set on the task at creation time
        (``customBranchName`` in the spec's metadata, e.g. ``hotfix/32_task``).
        Falls back to the default ``feature/{spec_name}`` namespace when no
        valid custom name is set.
        """
        custom = self._read_custom_branch_name(spec_name)
        return custom or f"feature/{spec_name}"

    def _read_custom_branch_name(self, spec_name: str) -> str | None:
        """Read a user-supplied custom branch name from the spec's metadata.

        Checks ``task_metadata.json`` first (the canonical runtime metadata),
        then ``requirements.json["metadata"]`` as a fallback. Returns a
        sanitized, git-valid branch name, or ``None`` when unset/invalid.
        """
        spec_dir = self.specs_dir / spec_name
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
                sanitized = self._sanitize_branch_name(str(raw))
                if sanitized:
                    return sanitized
        return None

    @staticmethod
    def _sanitize_branch_name(raw: str) -> str | None:
        """Coerce a user string into a valid git branch name, or ``None``.

        Trims, turns whitespace into hyphens, strips characters git forbids in
        ref names, and rejects names git would refuse. Keeps user-chosen
        prefixes like ``hotfix/`` intact.
        """
        name = raw.strip()
        if not name:
            return None
        # Whitespace -> hyphen
        name = re.sub(r"\s+", "-", name)
        # Drop characters git disallows in ref names (space ~ ^ : ? * [ \ DEL + control chars)
        name = re.sub(r"[~^:?*\[\\\x00-\x1f\x7f]", "", name)
        # Collapse repeated slashes; trim leading/trailing slashes and dots
        name = re.sub(r"/{2,}", "/", name).strip("/.")
        # Reject sequences git forbids outright
        if not name or ".." in name or "@{" in name or name.endswith(".lock"):
            return None
        # Reject any path component that starts with a dot (e.g. ".git", "foo/.bar")
        if any(part.startswith(".") or part == "" for part in name.split("/")):
            return None
        return name

    def worktree_exists(self, spec_name: str) -> bool:
        """Check if a worktree exists for a spec."""
        return self.get_worktree_path(spec_name).exists()

    def get_worktree_info(self, spec_name: str) -> WorktreeInfo | None:
        """Get info about a spec's worktree."""
        worktree_path = self.get_worktree_path(spec_name)
        if not worktree_path.exists():
            return None

        # Verify this is a real git worktree (has .git FILE, not directory)
        # Git worktrees have a .git file that points to the main repo's .git/worktrees/
        # A regular directory inside the repo would not have this
        git_path = worktree_path / ".git"
        if not git_path.exists() or not git_path.is_file():
            # Directory exists but is not a valid worktree
            return None

        # Verify the branch exists in the worktree
        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path)
        if result.returncode != 0:
            return None

        actual_branch = result.stdout.strip()

        # Get statistics
        stats = self._get_worktree_stats(spec_name)

        return WorktreeInfo(
            path=worktree_path,
            branch=actual_branch,
            spec_name=spec_name,
            base_branch=self.base_branch,
            is_active=True,
            **stats,
        )

    def _check_branch_namespace_conflict(self) -> str | None:
        """
        Check if a branch named 'feature' exists, which would block creating
        branches in the 'feature/*' namespace.

        Git stores branch refs as files under .git/refs/heads/, so a branch named
        'feature' creates a file that prevents creating the 'feature/'
        directory needed for 'feature/{spec-name}' branches.

        Returns:
            The conflicting branch name if found, None otherwise.
        """
        result = self._run_git(["rev-parse", "--verify", "feature"])
        if result.returncode == 0:
            return "feature"
        return None

    def _get_worktree_stats(self, spec_name: str) -> dict:
        """Get diff statistics for a worktree."""
        worktree_path = self.get_worktree_path(spec_name)

        stats = {
            "commit_count": 0,
            "files_changed": 0,
            "additions": 0,
            "deletions": 0,
        }

        if not worktree_path.exists():
            return stats

        # Commit count
        result = self._run_git(
            ["rev-list", "--count", f"{self.base_branch}..HEAD"], cwd=worktree_path
        )
        if result.returncode == 0:
            stats["commit_count"] = int(result.stdout.strip() or "0")

        # Diff stats
        result = self._run_git(
            ["diff", "--shortstat", f"{self.base_branch}...HEAD"], cwd=worktree_path
        )
        if result.returncode == 0 and result.stdout.strip():
            # Parse: "3 files changed, 50 insertions(+), 10 deletions(-)"
            match = re.search(r"(\d+) files? changed", result.stdout)
            if match:
                stats["files_changed"] = int(match.group(1))
            match = re.search(r"(\d+) insertions?", result.stdout)
            if match:
                stats["additions"] = int(match.group(1))
            match = re.search(r"(\d+) deletions?", result.stdout)
            if match:
                stats["deletions"] = int(match.group(1))

        return stats

    def create_worktree(self, spec_name: str) -> WorktreeInfo:
        """
        Create a worktree for a spec.

        Args:
            spec_name: The spec folder name (e.g., "002-implement-memory")

        Returns:
            WorktreeInfo for the created worktree

        Raises:
            WorktreeError: If a branch namespace conflict exists or worktree creation fails
        """
        worktree_path = self.get_worktree_path(spec_name)
        branch_name = self.get_branch_name(spec_name)

        # Check for branch namespace conflict (e.g., 'feature' blocking 'feature/*')
        conflicting_branch = self._check_branch_namespace_conflict()
        if conflicting_branch:
            raise WorktreeError(
                f"Branch '{conflicting_branch}' exists and blocks creating '{branch_name}'.\n"
                f"\n"
                f"Git branch names work like file paths - a branch named 'feature' prevents\n"
                f"creating branches under 'feature/' (like 'feature/{spec_name}').\n"
                f"\n"
                f"Fix: Rename the conflicting branch:\n"
                f"  git branch -m {conflicting_branch} {conflicting_branch}-backup"
            )

        # Remove existing if present (from crashed previous run or pre-created directory)
        if worktree_path.exists():
            # Check if it's a real worktree (has .git file)
            git_path = worktree_path / ".git"
            if git_path.exists() and git_path.is_file():
                # Real worktree - use git worktree remove
                self._run_git(["worktree", "remove", "--force", str(worktree_path)])
            else:
                # Not a real worktree (e.g., pre-created by agent_service)
                # Just delete the directory
                shutil.rmtree(worktree_path, ignore_errors=True)

        # Delete branch if it exists (from previous attempt)
        self._run_git(["branch", "-D", branch_name])

        # Cut from the latest remote state of the base branch when reachable
        # (falls back to the local base if there's no remote / fetch fails).
        start_point = fetch_base_start_point(self.project_dir, self.base_branch)

        # Create worktree with new branch from the resolved start point
        result = self._run_git(
            ["worktree", "add", "-b", branch_name, str(worktree_path), start_point]
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to create worktree for {spec_name}: {result.stderr}"
            )

        print(f"Created worktree: {worktree_path.name} on branch {branch_name}")
        logger.info(f"Created worktree for spec '{spec_name}'", extra={
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
            "base_branch": self.base_branch,
        })

        return WorktreeInfo(
            path=worktree_path,
            branch=branch_name,
            spec_name=spec_name,
            base_branch=self.base_branch,
            is_active=True,
        )

    def get_or_create_worktree(self, spec_name: str) -> WorktreeInfo:
        """
        Get existing worktree or create a new one for a spec.

        Args:
            spec_name: The spec folder name

        Returns:
            WorktreeInfo for the worktree
        """
        existing = self.get_worktree_info(spec_name)
        if existing:
            print(f"Using existing worktree: {existing.path}")
            logger.info(f"Using existing worktree for spec '{spec_name}'", extra={
                "worktree_path": str(existing.path),
                "branch": existing.branch,
            })
            return existing

        return self.create_worktree(spec_name)

    def remove_worktree(self, spec_name: str, delete_branch: bool = False) -> None:
        """
        Remove a spec's worktree.

        Args:
            spec_name: The spec folder name
            delete_branch: Whether to also delete the branch
        """
        worktree_path = self.get_worktree_path(spec_name)
        branch_name = self.get_branch_name(spec_name)

        if worktree_path.exists():
            result = self._run_git(
                ["worktree", "remove", "--force", str(worktree_path)]
            )
            if result.returncode == 0:
                print(f"Removed worktree: {worktree_path.name}")
                logger.info(f"Removed worktree for spec '{spec_name}'", extra={
                    "worktree_path": str(worktree_path),
                })
            else:
                print(f"Warning: Could not remove worktree: {result.stderr}")
                logger.warning("Could not remove worktree via git, falling back to rmtree", extra={
                    "worktree_path": str(worktree_path),
                    "error": result.stderr,
                })
                shutil.rmtree(worktree_path, ignore_errors=True)

        if delete_branch:
            self._run_git(["branch", "-D", branch_name])
            print(f"Deleted branch: {branch_name}")
            logger.info(f"Deleted branch '{branch_name}'")

        self._run_git(["worktree", "prune"])

    def merge_worktree(
        self, spec_name: str, delete_after: bool = False, no_commit: bool = False
    ) -> bool:
        """
        Merge a spec's worktree branch back to base branch.

        Args:
            spec_name: The spec folder name
            delete_after: Whether to remove worktree and branch after merge
            no_commit: If True, merge changes but don't commit (stage only for review)

        Returns:
            True if merge succeeded
        """
        info = self.get_worktree_info(spec_name)
        if not info:
            print(f"No worktree found for spec: {spec_name}")
            logger.warning(f"Merge attempted but no worktree found for spec '{spec_name}'")
            return False

        if no_commit:
            print(
                f"Merging {info.branch} into {self.base_branch} (staged, not committed)..."
            )
            logger.info(f"Starting staged merge (no-commit) for spec '{spec_name}'", extra={
                "branch": info.branch,
                "base_branch": self.base_branch,
            })
        else:
            print(f"Merging {info.branch} into {self.base_branch}...")
            logger.info(f"Starting merge for spec '{spec_name}'", extra={
                "branch": info.branch,
                "base_branch": self.base_branch,
            })

        # Clean up internal auto-generated files that can block merge/checkout.
        # These are untracked files created by agents that would collide with
        # the same untracked files coming from the worktree branch.
        _INTERNAL_MERGE_BLOCKERS = [
            ".magestic-ai-security.json",
            ".magestic-ai-status",
        ]
        for fname in _INTERNAL_MERGE_BLOCKERS:
            blocker = self.project_dir / fname
            if blocker.exists():
                try:
                    blocker.unlink()
                    logger.info(f"Removed merge-blocking file: {fname}")
                except OSError:
                    pass

        # Switch to base branch in main project
        result = self._run_git(["checkout", self.base_branch])
        if result.returncode != 0:
            print(f"Error: Could not checkout base branch: {result.stderr}")
            logger.error(f"Could not checkout base branch '{self.base_branch}' for merge", extra={
                "spec_name": spec_name,
                "error": result.stderr,
            })
            return False

        # Merge the spec branch
        merge_args = ["merge", "--no-ff", info.branch]
        if no_commit:
            # --no-commit stages the merge but doesn't create the commit
            merge_args.append("--no-commit")
        else:
            merge_args.extend(["-m", f"Merge {info.branch}"])

        result = self._run_git(merge_args)

        if result.returncode != 0:
            print("Merge conflict! Aborting merge...")
            logger.error(f"Merge conflict detected for spec '{spec_name}', aborting", extra={
                "branch": info.branch,
                "base_branch": self.base_branch,
                "error": result.stderr,
            })
            self._run_git(["merge", "--abort"])
            return False

        if no_commit:
            # Unstage any files that are gitignored in the main branch
            # These get staged during merge because they exist in the worktree branch
            self._unstage_gitignored_files()
            print(
                f"Changes from {info.branch} are now staged in your working directory."
            )
            print("Review the changes, then commit when ready:")
            print("  git commit -m 'your commit message'")
            logger.info(f"Staged merge completed for spec '{spec_name}' (no-commit mode)", extra={
                "branch": info.branch,
                "base_branch": self.base_branch,
            })
        else:
            print(f"Successfully merged {info.branch}")
            logger.info(f"Successfully merged spec '{spec_name}'", extra={
                "branch": info.branch,
                "base_branch": self.base_branch,
            })

        if delete_after:
            self.remove_worktree(spec_name, delete_branch=True)

        return True

    def commit_in_worktree(self, spec_name: str, message: str) -> bool:
        """Commit all changes in a spec's worktree."""
        worktree_path = self.get_worktree_path(spec_name)
        if not worktree_path.exists():
            return False

        self._run_git(["add", ".", ":!.magestic-ai"], cwd=worktree_path)
        result = self._run_git(["commit", "-m", message], cwd=worktree_path)

        if result.returncode == 0:
            return True
        elif "nothing to commit" in result.stdout + result.stderr:
            return True
        else:
            print(f"Commit failed: {result.stderr}")
            logger.error(f"Commit failed in worktree for spec '{spec_name}'", extra={
                "worktree_path": str(worktree_path),
                "error": result.stderr,
                "message": message,
            })
            return False

    # ==================== Listing & Discovery ====================

    def list_all_worktrees(self) -> list[WorktreeInfo]:
        """List all spec worktrees."""
        worktrees = []

        if self.worktrees_dir.exists():
            for item in self.worktrees_dir.iterdir():
                if item.is_dir():
                    info = self.get_worktree_info(item.name)
                    if info:
                        worktrees.append(info)

        return worktrees

    def list_all_spec_branches(self) -> list[str]:
        """List all feature branches (even if worktree removed)."""
        result = self._run_git(["branch", "--list", "feature/*"])
        if result.returncode != 0:
            return []

        branches = []
        for line in result.stdout.strip().split("\n"):
            branch = line.strip().lstrip("* ")
            if branch:
                branches.append(branch)

        return branches

    def get_changed_files(self, spec_name: str) -> list[tuple[str, str]]:
        """Get list of changed files in a spec's worktree."""
        worktree_path = self.get_worktree_path(spec_name)
        if not worktree_path.exists():
            return []

        result = self._run_git(
            ["diff", "--name-status", f"{self.base_branch}...HEAD"], cwd=worktree_path
        )

        files = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                files.append((parts[0], parts[1]))

        return files

    def get_change_summary(self, spec_name: str) -> dict:
        """Get a summary of changes in a worktree."""
        files = self.get_changed_files(spec_name)

        new_files = sum(1 for status, _ in files if status == "A")
        modified_files = sum(1 for status, _ in files if status == "M")
        deleted_files = sum(1 for status, _ in files if status == "D")

        return {
            "new_files": new_files,
            "modified_files": modified_files,
            "deleted_files": deleted_files,
        }

    def cleanup_all(self) -> None:
        """Remove all worktrees and their branches."""
        for worktree in self.list_all_worktrees():
            self.remove_worktree(worktree.spec_name, delete_branch=True)

    def cleanup_stale_worktrees(self) -> None:
        """Remove worktrees that aren't registered with git."""
        if not self.worktrees_dir.exists():
            return

        # Get list of registered worktrees
        result = self._run_git(["worktree", "list", "--porcelain"])
        registered_paths = set()
        for line in result.stdout.split("\n"):
            if line.startswith("worktree "):
                registered_paths.add(Path(line.split(" ", 1)[1]))

        # Remove unregistered directories
        for item in self.worktrees_dir.iterdir():
            if item.is_dir() and item not in registered_paths:
                print(f"Removing stale worktree directory: {item.name}")
                shutil.rmtree(item, ignore_errors=True)

        self._run_git(["worktree", "prune"])

    def get_test_commands(self, spec_name: str) -> list[str]:
        """Detect likely test/run commands for the project."""
        worktree_path = self.get_worktree_path(spec_name)
        commands = []

        if (worktree_path / "package.json").exists():
            commands.append("npm install && npm run dev")
            commands.append("npm test")

        if (worktree_path / "requirements.txt").exists():
            commands.append("pip install -r requirements.txt")

        if (worktree_path / "Cargo.toml").exists():
            commands.append("cargo run")
            commands.append("cargo test")

        if (worktree_path / "go.mod").exists():
            commands.append("go run .")
            commands.append("go test ./...")

        if not commands:
            commands.append("# Check the project's README for run instructions")

        return commands

    def has_uncommitted_changes(self, spec_name: str | None = None) -> bool:
        """Check if there are uncommitted changes."""
        cwd = None
        if spec_name:
            worktree_path = self.get_worktree_path(spec_name)
            if worktree_path.exists():
                cwd = worktree_path
        result = self._run_git(["status", "--porcelain"], cwd=cwd)
        return bool(result.stdout.strip())
