"""WebSocket endpoint bridging a browser Monaco editor to a language server.

The browser side (monaco-languageclient via vscode-ws-jsonrpc) sends/receives
bare JSON-RPC messages as WS text frames; ``LSPSession`` translates them to/from
the language server's Content-Length-framed stdio.

Connect with: ``/ws/lsp/{language}?root=<abs workspace path>&token=<jwt>``

Close codes:
- 4001 unauthorized
- 4002 missing / invalid / unauthorized workspace root
- 4003 unsupported language
- 4501 language server not installed
- 4502 language server exited
- 4503 server capacity reached
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import verify_websocket_token
from ..lsp.manager import LANGUAGE_SERVERS, get_lsp_manager, resolve_command

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_root(root: str) -> Path | None:
    """Resolve ``root`` and require it to be a registered project (or a subdir).

    Without this an arbitrary path could be used as a workspace — and tsserver
    *executes* the workspace ``tsconfig``/plugins, so an untrusted root is code
    execution + arbitrary file reads. Mirrors the containment check in
    ``files.resolve_path``.
    """
    try:
        root_path = Path(root).resolve()
    except Exception:
        return None
    if not root_path.exists() or not root_path.is_dir():
        return None

    try:
        from ..routes.projects import load_projects  # local import avoids cycle

        projects = load_projects()
    except Exception:
        projects = {}

    for proj in projects.values():
        proj_path = proj.get("path")
        if not proj_path:
            continue
        try:
            allowed = Path(proj_path).resolve()
        except Exception:
            continue
        if root_path == allowed:
            return root_path
        try:
            root_path.relative_to(allowed)
            return root_path
        except ValueError:
            continue
    return None


@router.websocket("/ws/lsp/{language}")
async def lsp_websocket(websocket: WebSocket, language: str):
    """Bridge a language server over a WebSocket for the given language."""
    if not await verify_websocket_token(websocket):
        return

    # Accept once up front: close *reasons* are only delivered to the client
    # after the handshake completes, so the frontend can read "not installed" etc.
    await websocket.accept()

    root_raw = websocket.query_params.get("root")
    if not root_raw:
        await websocket.close(code=4002, reason="Missing 'root' query parameter")
        return

    language = language.lower()
    if language not in LANGUAGE_SERVERS:
        await websocket.close(code=4003, reason=f"Unsupported language: {language}")
        return

    root_path = _validate_root(root_raw)
    if root_path is None:
        await websocket.close(code=4002, reason="Invalid or unauthorized workspace root")
        return

    command, reason = resolve_command(language)
    if command is None:
        await websocket.close(code=4501, reason=reason or "Language server not installed")
        return

    manager = get_lsp_manager()
    try:
        session = await manager.create_session(language, command, str(root_path))
    except RuntimeError as exc:
        await websocket.close(code=4503, reason=str(exc))
        return
    except Exception as exc:
        logger.exception("[lsp] failed to start %s server", language)
        await websocket.close(code=4502, reason=f"Failed to start language server: {exc}")
        return

    async def send_to_ws(text: str) -> None:
        try:
            await websocket.send_text(text)
        except Exception:
            pass

    stdout_task = asyncio.create_task(session.pump_stdout_to_ws(send_to_ws))
    stderr_task = asyncio.create_task(session.drain_stderr())

    close_code = 1000
    try:
        while True:
            receive_task = asyncio.ensure_future(websocket.receive())
            done, _ = await asyncio.wait(
                {receive_task, stdout_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Server process closed stdout (exited) — tear down the socket.
            if stdout_task in done:
                receive_task.cancel()
                try:
                    await receive_task
                except (asyncio.CancelledError, WebSocketDisconnect, Exception):
                    pass
                close_code = 4502
                break

            message = receive_task.result()
            if message["type"] == "websocket.disconnect":
                break

            text = message.get("text")
            if text is not None:
                await session.write_message(text)
                continue
            data = message.get("bytes")
            if data is not None:
                await session.write_message(data.decode("utf-8", "replace"))

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[lsp:%s] receive loop error: %s", language, exc)

    finally:
        for task in (stdout_task, stderr_task):
            task.cancel()
        for task in (stdout_task, stderr_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        await manager.close_session(session.id)

        try:
            await websocket.close(code=close_code)
        except Exception:
            pass
