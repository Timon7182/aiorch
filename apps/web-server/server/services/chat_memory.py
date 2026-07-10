"""Long-term chat memory backed by the project's Graphiti knowledge graph.

Bridges the web-server insights chat into the backend ``GraphitiMemory``
(in-process), scoped per project so a conversation can recall facts learned in
earlier chats. Two operations:

  - ``recall(project, query)``  -> a short context block to prepend to the turn
  - ``store(project, user, assistant)`` -> persist the exchange as an episode

Fully gated and defensive: a complete no-op unless ``GRAPHITI_ENABLED`` is set
and the backend graphiti package imports. Nothing here raises into the chat
path — every failure degrades to "no memory this turn".

The embedded LadybugDB driver cannot reliably reopen its WAL within the same
process after a close, so each project's ``GraphitiMemory`` is opened once and
**cached for the process lifetime** (never closed between turns). A per-project
lock serialises operations on the shared DB handle.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_backend_on_path = False
# project key -> open GraphitiMemory (kept open; see module docstring)
_memories: dict[str, object] = {}
# project key -> lock serialising ops on that project's DB handle
_locks: dict[str, asyncio.Lock] = {}
# guards first-time creation/initialisation of a project's memory
_init_lock = asyncio.Lock()


def _ensure_backend_on_path() -> bool:
    """Put apps/backend on sys.path so `integrations.graphiti` imports."""
    global _backend_on_path
    if _backend_on_path:
        return True
    # services/chat_memory.py -> server -> web-server -> apps -> apps/backend
    backend = Path(__file__).resolve().parents[3] / "backend"
    if not backend.is_dir():
        logger.info("[ChatMemory] backend dir not found at %s", backend)
        return False
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    _backend_on_path = True
    return True


def is_enabled() -> bool:
    """True only when Graphiti is switched on and the backend is importable."""
    if os.environ.get("GRAPHITI_ENABLED", "").strip().lower() not in ("true", "1", "yes"):
        return False
    return _ensure_backend_on_path()


async def _get_memory(project_path: Path):
    """Return a cached, initialised project-scoped GraphitiMemory, or None.

    Opened once and kept open (PROJECT group-id mode = one shared memory for all
    chats in this project). Concurrent first-callers are serialised by
    ``_init_lock``; a failed init is not cached, so a later turn can retry.
    """
    if not is_enabled():
        return None
    key = str(project_path.resolve())
    cached = _memories.get(key)
    if cached is not None:
        return cached
    async with _init_lock:
        cached = _memories.get(key)
        if cached is not None:
            return cached
        try:
            from integrations.graphiti import GraphitiMemory
            from integrations.graphiti.queries_pkg.schema import GroupIdMode
        except Exception as e:  # pragma: no cover - import shape varies by deploy
            logger.info("[ChatMemory] graphiti import unavailable: %s", e)
            return None
        try:
            spec_dir = project_path / ".magestic-ai"
            spec_dir.mkdir(parents=True, exist_ok=True)
            memory = GraphitiMemory(spec_dir, project_path, group_id_mode=GroupIdMode.PROJECT)
            if not memory.is_enabled:
                return None
            if not await memory.initialize():
                logger.info("[ChatMemory] graphiti initialize() returned False")
                return None
            _memories[key] = memory
            _locks[key] = asyncio.Lock()
            return memory
        except Exception as e:
            logger.info("[ChatMemory] init failed: %s", e)
            return None


def _format(items) -> str:
    """Render recalled context items into a compact, model-facing block."""
    lines: list[str] = []
    for item in items or []:
        content = ""
        if isinstance(item, dict):
            content = (item.get("content") or "").strip()
        if content:
            lines.append(f"- {content}")
    if not lines:
        return ""
    return (
        "Relevant facts recalled from this project's long-term memory "
        "(use if helpful, ignore if not):\n" + "\n".join(lines[:8])
    )


async def recall(project_path: Path, query: str, max_items: int = 5) -> str:
    """Return a context block of facts relevant to ``query``, or ""."""
    if not query or not query.strip():
        return ""
    memory = await _get_memory(project_path)
    if not memory:
        return ""
    key = str(project_path.resolve())
    try:
        async with _locks[key]:
            items = await memory.get_relevant_context(query, num_results=max_items)
        return _format(items)
    except Exception as e:
        logger.info("[ChatMemory] recall failed: %s", e)
        return ""


async def store(project_path: Path, user_text: str, assistant_text: str) -> None:
    """Persist a chat exchange as an episode. Best-effort, never raises."""
    memory = await _get_memory(project_path)
    if not memory:
        return
    key = str(project_path.resolve())
    try:
        body = (
            f"User: {(user_text or '').strip()}\n\n"
            f"Assistant: {(assistant_text or '').strip()}"
        )
        # Cap the body so a long answer doesn't blow up ingestion cost/time.
        async with _locks[key]:
            await memory.save_chat_episode(body[:8000], name_hint="turn")
    except Exception as e:
        logger.info("[ChatMemory] store failed: %s", e)
