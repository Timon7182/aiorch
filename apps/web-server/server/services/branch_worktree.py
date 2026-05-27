"""Read-only branch worktrees for insights chat.

The insights chat shells out to a provider CLI (`claude`, `codex`, ...) with
``cwd`` set to the project directory, so it answers using whatever is *currently
checked out*. When the user wants the chat grounded in a *different* branch
("the info I need lives in branch X"), we can't `git checkout` the user's
working tree — that would clobber their uncommitted work.

Instead we materialise the branch in a **detached** worktree under
``.magestic-ai/worktrees/insights/<branch>`` (``.magestic-ai`` is gitignored)
and run the chat with ``cwd`` pointed there. ``--detach`` avoids git's
"branch already checked out elsewhere" error when the same branch is also live
in the main worktree, and read-only chat never needs the branch ref anyway.

The worktree is reused across messages and fast-forwarded to the branch tip on
each request, so repeat questions against the same branch don't pay the
checkout cost twice.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 60

# Cap how many branch worktrees we keep around per project. Each one is a full
# checkout, so without a ceiling, chatting across many branches would grow disk
# use without bound. Least-recently-used worktrees beyond this are removed.
_MAX_INSIGHTS_WORKTREES = 5


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # safe.directory=* so git works on repos owned by a different uid than the
    # process (dockerized deploy: root process, host-owned bind-mounted repos).
    # Without it git aborts with "detected dubious ownership" and the branch
    # worktree silently fails to build.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def _sanitize(branch: str) -> str:
    """Turn a branch name into a filesystem-safe directory name."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", branch).strip("-") or "branch"


def _current_branch(project_path: Path) -> str | None:
    result = _run_git(["branch", "--show-current"], project_path)
    if result.returncode == 0:
        return result.stdout.strip() or None
    return None


def _insights_worktrees_root(project_path: Path) -> Path:
    return project_path / ".magestic-ai" / "worktrees" / "insights"


def _touch(path: Path) -> None:
    """Mark a worktree as just-used so LRU pruning keeps it around."""
    try:
        os.utime(path, (time.time(), time.time()))
    except OSError:
        pass


def cleanup_insights_worktrees(project_path: Path, keep: int = _MAX_INSIGHTS_WORKTREES) -> None:
    """Prune stale registrations and cap insights worktrees to the newest ``keep``.

    "Newest" is by directory mtime, which :func:`ensure_branch_worktree` bumps on
    every use — so the worktrees the user actually keeps chatting against survive
    and idle ones get reclaimed. Always best-effort: failures are logged, never
    raised, since cleanup must not break a chat turn.
    """
    try:
        # Drop registrations whose directory git can no longer find first.
        _run_git(["worktree", "prune"], project_path)

        root = _insights_worktrees_root(project_path)
        if not root.is_dir():
            return

        dirs = [d for d in root.iterdir() if d.is_dir()]
        if len(dirs) <= keep:
            return

        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for stale in dirs[keep:]:
            removed = _run_git(["worktree", "remove", "--force", str(stale)], project_path)
            if removed.returncode != 0:
                # Not a registered worktree (or git refused) — delete the tree
                # directly so it can't linger.
                shutil.rmtree(stale, ignore_errors=True)
            logger.info("[branch_worktree] pruned idle worktree %s", stale.name)

        _run_git(["worktree", "prune"], project_path)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("[branch_worktree] cleanup skipped: %s", exc)


def _branch_exists(project_path: Path, branch: str) -> bool:
    # Match a local branch by its full ref so names like "feature/x" resolve
    # exactly and we never accidentally pick up a tag of the same name.
    if _run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], project_path
    ).returncode == 0:
        return True
    # Also accept a remote-tracking ref (e.g. "origin/dev"). get_git_branches
    # surfaces remote-only branches, and `git worktree add --detach <ref>` can
    # check them out, so the chat can ground in a branch that isn't local yet.
    return _run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/remotes/{branch}"], project_path
    ).returncode == 0


def predicted_ground_dir(project_path: Path, branch: str | None) -> Path:
    """Where the chat *will* run for ``(project_path, branch)`` — without
    creating anything.

    Mirrors :func:`ensure_branch_worktree`'s directory choice so availability
    checks (e.g. "is CodeGraph indexed for this branch?") inspect the same dir
    the provider will actually use:

    * no branch / the current checkout / an unknown branch -> ``project_path``
      (``ensure_branch_worktree`` returns ``None`` in all three cases, and the
      caller falls back to the project dir)
    * any other existing branch -> its deterministic insights-worktree path

    Note this is purely a path computation: a not-yet-created worktree won't
    contain ``.codegraphcontext/`` (it's gitignored), which is exactly why
    CodeGraph is effectively current-checkout-only.
    """
    if not branch or not branch.strip():
        return project_path
    branch = branch.strip()
    if branch == _current_branch(project_path):
        return project_path
    if not _branch_exists(project_path, branch):
        return project_path
    return project_path / ".magestic-ai" / "worktrees" / "insights" / _sanitize(branch)


def ensure_branch_worktree(project_path: Path, branch: str | None) -> Path | None:
    """Return a worktree directory checked out at ``branch``, or ``None``.

    ``None`` means "use the project directory as-is" — returned when no branch
    is requested, when the requested branch is already the current checkout, or
    when anything about the worktree setup fails. Callers must treat ``None`` as
    "fall back to ``project_path``" so chat never hard-fails over branch
    selection.
    """
    if not branch or not branch.strip():
        return None

    branch = branch.strip()

    # No point spinning up a worktree for the branch the user is already on.
    if branch == _current_branch(project_path):
        return None

    if not _branch_exists(project_path, branch):
        logger.warning("[branch_worktree] branch %r not found in %s", branch, project_path)
        return None

    worktree_dir = project_path / ".magestic-ai" / "worktrees" / "insights" / _sanitize(branch)

    try:
        if (worktree_dir / ".git").exists():
            # Reuse: move the detached HEAD to the branch tip so the chat sees
            # the latest commits. If the refresh fails (e.g. the registration
            # went stale), tear it down and recreate from scratch below.
            refresh = _run_git(["checkout", "--detach", branch], worktree_dir)
            if refresh.returncode == 0:
                _touch(worktree_dir)
                return worktree_dir
            logger.info(
                "[branch_worktree] refresh failed (%s); recreating",
                refresh.stderr.strip(),
            )
            _run_git(["worktree", "remove", "--force", str(worktree_dir)], project_path)
        elif worktree_dir.exists():
            # Directory exists but isn't a worktree (partial/corrupt state):
            # `git worktree add` would refuse the path, so clear it first.
            shutil.rmtree(worktree_dir, ignore_errors=True)

        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        # Prune any stale registration left behind by a deleted directory so
        # `worktree add` doesn't refuse the path.
        _run_git(["worktree", "prune"], project_path)

        add = _run_git(
            ["worktree", "add", "--detach", str(worktree_dir), branch], project_path
        )
        if add.returncode != 0:
            logger.warning(
                "[branch_worktree] worktree add failed for %r: %s",
                branch,
                add.stderr.strip(),
            )
            return None

        _touch(worktree_dir)
        # Reclaim idle worktrees now that we've added a fresh one.
        cleanup_insights_worktrees(project_path)
        return worktree_dir
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("[branch_worktree] setup failed for %r: %s", branch, exc)
        return None
