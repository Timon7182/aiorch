"""LSP Manager - lifecycle for language-server sessions.

Holds the language allowlist, resolves server binaries (mirroring the
``_resolve_codegraph_bin`` precedent: explicit env var -> venv scripts dir ->
PATH), and manages one server process per WebSocket connection.
"""

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

from ..config import get_settings
from .session import LSPSession

logger = logging.getLogger(__name__)

# Supported languages -> which server binary + launch args.
# typescript-language-server handles JS/TS/JSX/TSX, so both ids map to it.
LANGUAGE_SERVERS: dict[str, dict] = {
    "python": {"bin_key": "pyright", "args": ["--stdio"]},
    "typescript": {"bin_key": "typescript", "args": ["--stdio"]},
    "javascript": {"bin_key": "typescript", "args": ["--stdio"]},
}

# Server binary -> env override var + candidate executable names. The ``.cmd``/
# ``.exe`` variants cover npm shims on the Windows dev box.
LSP_BINARIES: dict[str, dict] = {
    "pyright": {
        "env": "LSP_PYRIGHT_BIN",
        "names": ["pyright-langserver", "pyright-langserver.cmd", "pyright-langserver.exe"],
    },
    "typescript": {
        "env": "LSP_TS_BIN",
        "names": [
            "typescript-language-server",
            "typescript-language-server.cmd",
            "typescript-language-server.exe",
        ],
    },
}


def _resolve_lsp_bin(bin_key: str) -> str | None:
    """Find a language-server executable.

    Resolution order mirrors ``_resolve_codegraph_bin`` in
    ``services/docs_generator_service.py``:
    1. explicit env override (``LSP_PYRIGHT_BIN`` / ``LSP_TS_BIN``) — absolute path
       used by the dockerized deploy / local overrides;
    2. co-located in the web-server venv scripts dir;
    3. PATH lookup (picks up the npm global shim).
    """
    spec = LSP_BINARIES.get(bin_key)
    if spec is None:
        return None

    explicit = os.environ.get(spec["env"])
    if explicit and Path(explicit).exists():
        return explicit

    scripts_dir = Path(sys.executable).parent
    for name in spec["names"]:
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)

    for name in spec["names"]:
        which = shutil.which(name)
        if which:
            return which

    return None


def resolve_command(language: str) -> tuple[list[str] | None, str | None]:
    """Map a language id to a launch command.

    Returns ``(command, None)`` on success or ``(None, reason)`` when the
    language is unsupported or the server binary is not installed.
    """
    server = LANGUAGE_SERVERS.get(language)
    if server is None:
        return None, f"Unsupported language: {language}"

    bin_path = _resolve_lsp_bin(server["bin_key"])
    if bin_path is None:
        return None, f"{language} language server not installed"

    return [bin_path, *server["args"]], None


class LSPManager:
    """Manages language-server sessions (one process per WebSocket connection)."""

    def __init__(self):
        self.sessions: dict[str, LSPSession] = {}
        self._lock = asyncio.Lock()
        self._max_servers = get_settings().MAX_LSP_SERVERS

    async def create_session(self, language: str, command: list[str], root: str) -> LSPSession:
        """Spawn a new language-server session. Raises RuntimeError when at capacity."""
        async with self._lock:
            self._cleanup_dead_locked()
            if len(self.sessions) >= self._max_servers:
                raise RuntimeError(f"Maximum LSP servers ({self._max_servers}) reached")

            session = LSPSession(language=language, command=command, root=root)
            await session.start()
            self.sessions[session.id] = session
            return session

    def get_session(self, session_id: str) -> LSPSession | None:
        return self.sessions.get(session_id)

    async def close_session(self, session_id: str) -> bool:
        async with self._lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close_all(self) -> int:
        async with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            await session.close()
        return len(sessions)

    def _cleanup_dead_locked(self) -> int:
        """Drop sessions whose processes have exited. Caller holds the lock."""
        dead = [sid for sid, session in self.sessions.items() if not session.is_alive()]
        for sid in dead:
            del self.sessions[sid]
        return len(dead)


_lsp_manager: LSPManager | None = None


def get_lsp_manager() -> LSPManager:
    """Get the global LSP manager instance."""
    global _lsp_manager
    if _lsp_manager is None:
        _lsp_manager = LSPManager()
    return _lsp_manager
