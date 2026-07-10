"""Optional filesystem watcher that auto-refreshes docs on code changes.

Opt-in via ``DOCS_WATCH_ENABLED=true`` (default off). When enabled, the server
lifespan snapshots the currently-registered project paths at startup and starts:

1. One ``watchfiles.awatch`` task per project. Source-file changes (filtered to
   ignore ``.git/``, ``.magestic-ai/``, ``node_modules/``, ``graphify-out/``,
   ``.codegraphcontext/`` and any ``docs-site`` dir) are debounced ~30s and then
   trigger a **CodeGraphContext re-index only** — cheap, no LLM tokens.

2. One periodic poll task that looks for the ``.magestic-ai/.docs-refresh-requested``
   touch-file each project's post-commit hook can drop (see the install-hook
   route). When present it is deleted and a full change-aware docs refresh runs.

Known limitation (documented on purpose): the project list is a **startup
snapshot**. Projects added after the server starts are not watched until the
next restart. This is intentional — the watcher is a best-effort convenience,
not a source of truth; the merge trigger covers the in-app path, and a restart
picks up new projects.

Branch-worktree note: the watcher only ever refreshes the project's *current
checkout*. Docs generated for other branches live in branch worktrees that
share the insights LRU pool (.magestic-ai/worktrees/insights/, capped at 5 per
repo by branch_worktree.cleanup_insights_worktrees) and can be evicted when
idle — they are recreated on the next branch-scoped docs request, so the
watcher deliberately doesn't try to keep them fresh.

All work is best-effort: exceptions are logged, never raised, so a watcher
hiccup can't take down the server.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories whose changes should never trigger a refresh.
_IGNORE_DIRS = {
    ".git",
    ".magestic-ai",
    "node_modules",
    "graphify-out",
    ".codegraphcontext",
    "docs-site",
    ".venv",
    "__pycache__",
}

_DEBOUNCE_SECONDS = 30.0
_TOUCHFILE_POLL_SECONDS = 15.0
_TOUCHFILE_NAME = ".docs-refresh-requested"


def watch_enabled() -> bool:
    return str(os.environ.get("DOCS_WATCH_ENABLED", "")).lower() in ("true", "1", "yes")


def _is_ignored(path: str) -> bool:
    parts = Path(path).parts
    return any(seg in _IGNORE_DIRS for seg in parts)


class DocsWatcher:
    """Manages the awatch + touch-file-poll background tasks."""

    def __init__(self, project_paths: list[Path], backend_path: Path):
        self._projects = [p for p in project_paths if p.is_dir()]
        self._backend_path = backend_path
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if not self._projects:
            logger.info("[docs_watcher] no projects to watch")
            return
        for project in self._projects:
            self._tasks.append(asyncio.create_task(self._watch_project(project)))
        self._tasks.append(asyncio.create_task(self._poll_touchfiles()))
        logger.info("[docs_watcher] watching %d project(s)", len(self._projects))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    # ------------------------------------------------------------------ #

    def _service(self):
        from .docs_generator_service import get_docs_generator_service
        return get_docs_generator_service(self._backend_path)

    async def _watch_project(self, project: Path) -> None:
        """Debounced CGC re-index on source-file changes under ``project``."""
        try:
            from watchfiles import awatch
        except Exception:
            logger.warning("[docs_watcher] watchfiles not installed; watcher disabled")
            return

        try:
            async for changes in awatch(
                project,
                watch_filter=lambda _change, path: not _is_ignored(path),
                stop_event=self._stop,
                debounce=int(_DEBOUNCE_SECONDS * 1000),
                step=500,
            ):
                if not changes:
                    continue
                logger.info(
                    "[docs_watcher] %d change(s) in %s -> reindexing code graph",
                    len(changes), project,
                )
                try:
                    await self._service().index_codegraph(project)
                except Exception:
                    logger.warning("[docs_watcher] index failed for %s", project, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[docs_watcher] watch loop crashed for %s", project, exc_info=True)

    async def _poll_touchfiles(self) -> None:
        """Poll each project for the hook-dropped refresh touch-file."""
        while not self._stop.is_set():
            for project in self._projects:
                marker = project / ".magestic-ai" / _TOUCHFILE_NAME
                if not marker.exists():
                    continue
                try:
                    marker.unlink()
                except OSError:
                    pass
                logger.info("[docs_watcher] refresh requested for %s (touch-file)", project)
                svc = self._service()
                if not svc.docs_exist(project):
                    continue
                try:
                    token = await svc.resolve_oauth_token()
                    await svc.refresh_docs_incremental(
                        project_id=str(project),
                        project_path=project,
                        oauth_token=token,
                    )
                except Exception:
                    logger.warning(
                        "[docs_watcher] touch-file refresh failed for %s", project, exc_info=True
                    )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_TOUCHFILE_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass


_watcher: DocsWatcher | None = None


def start_watcher(project_paths: list[Path], backend_path: Path) -> None:
    """Start the singleton watcher if DOCS_WATCH_ENABLED is set."""
    global _watcher
    if not watch_enabled():
        return
    if _watcher is not None:
        return
    _watcher = DocsWatcher(project_paths, backend_path)
    _watcher.start()


async def stop_watcher() -> None:
    """Stop the singleton watcher (clean shutdown)."""
    global _watcher
    if _watcher is not None:
        await _watcher.stop()
        _watcher = None
