"""
Global Events WebSocket with per-client routing.

Supports both broadcast (legacy) and targeted delivery based on
user identity.  When a JWT-authenticated user connects, events
can be routed only to members of the relevant organization.
Legacy (bearer-token) connections receive all events (backward
compatible).
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import WebSocketAuthError, authenticate_websocket, verify_websocket_token

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Client tracking
# ---------------------------------------------------------------------------


@dataclass
class ConnectedClient:
    """A connected WebSocket client with optional identity."""

    websocket: WebSocket
    user_id: str | None = None
    org_ids: set[str] = field(default_factory=set)


# Active WebSocket connections — keyed by WebSocket object for fast lookup
_clients: dict[WebSocket, ConnectedClient] = {}

# The main asyncio loop, captured at app startup. Background *threads* (which have
# no running loop of their own — e.g. the remote preview-deploy worker) use this
# to schedule broadcasts back onto the loop the websockets live on.
_main_loop: "asyncio.AbstractEventLoop | None" = None


def set_main_loop(loop: "asyncio.AbstractEventLoop") -> None:
    """Record the running event loop so thread code can broadcast via it."""
    global _main_loop
    _main_loop = loop


def emit_threadsafe(coro) -> None:
    """Schedule a broadcast coroutine onto the main loop from any thread.

    Safe to call from a non-async background thread (paramiko workers, the
    preview-deploy thread). No-ops silently if the loop isn't available yet.
    """
    loop = _main_loop
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

    def _log_result(f: "asyncio.Future") -> None:
        if f.cancelled():
            return
        exc = f.exception()
        if exc is not None:
            logger.warning("emit_threadsafe broadcast failed: %s", exc)

    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        fut.add_done_callback(_log_result)
    except Exception:
        logger.debug("emit_threadsafe failed", exc_info=True)

# Legacy set kept for backward compatibility with code that still
# references ``active_connections`` directly.
active_connections: set[WebSocket] = set()


def _register_client(ws: WebSocket, user_info: dict | None) -> ConnectedClient:
    """Register a new client connection."""
    client = ConnectedClient(
        websocket=ws,
        user_id=user_info["id"] if user_info else None,
    )
    _clients[ws] = client
    active_connections.add(ws)
    return client


def _unregister_client(ws: WebSocket) -> None:
    """Remove a client connection."""
    _clients.pop(ws, None)
    active_connections.discard(ws)


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------


async def broadcast_event(event_type: str, payload: dict):
    """Broadcast an event to all connected clients (legacy behavior).

    Sends run concurrently with a per-client timeout. The old sequential
    ``await ws.send_text`` had head-of-line blocking: one slow/half-open
    client (full TCP send buffer) stalled the whole broadcast — and since
    providers await this inline in their streaming loops, it stalled the
    stream for every client too.
    """
    message = json.dumps({"type": event_type, "payload": payload})
    targets = list(active_connections)
    if not targets:
        return

    async def _send_one(ws: WebSocket) -> WebSocket | None:
        try:
            await asyncio.wait_for(ws.send_text(message), timeout=5)
            return None
        except Exception:
            return ws

    results = await asyncio.gather(*(_send_one(ws) for ws in targets))
    for ws in results:
        if ws is not None:
            _unregister_client(ws)


async def send_to_user(user_id: str, event_type: str, payload: dict):
    """Send an event to a specific user (all their connections)."""
    message = json.dumps({"type": event_type, "payload": payload})
    disconnected: list[WebSocket] = []

    for ws, client in list(_clients.items()):
        if client.user_id == user_id:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

    for ws in disconnected:
        _unregister_client(ws)


async def send_to_org(org_id: str, event_type: str, payload: dict):
    """Send an event only to members of a specific organization.

    Falls back to broadcast for legacy (non-JWT) connections so they
    aren't excluded.
    """
    message = json.dumps({"type": event_type, "payload": payload})
    disconnected: list[WebSocket] = []

    for ws, client in list(_clients.items()):
        # Send to: org members, or legacy clients (no user_id)
        if client.user_id is None or org_id in client.org_ids:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

    for ws in disconnected:
        _unregister_client(ws)


def update_client_orgs(user_id: str, org_ids: set[str]) -> None:
    """Update the org memberships for all connections of a given user.

    Call this after the user's org memberships change so routing
    reflects the new state.
    """
    for client in _clients.values():
        if client.user_id == user_id:
            client.org_ids = org_ids


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws/events")
async def events_websocket(websocket: WebSocket):
    """WebSocket endpoint for global events."""
    await websocket.accept()

    # Authenticate — get user info if JWT, None for legacy token
    try:
        user_info = await authenticate_websocket(websocket)
    except WebSocketAuthError:
        return

    client = _register_client(websocket, user_info)

    # If authenticated user, load their org memberships for routing
    if user_info and user_info.get("id"):
        try:
            from ..database.engine import async_session_factory
            from ..database import OrgMember
            from sqlalchemy import select

            async with async_session_factory() as session:
                result = await session.execute(
                    select(OrgMember.org_id).where(
                        OrgMember.user_id == user_info["id"]
                    )
                )
                client.org_ids = {row[0] for row in result.all()}
        except Exception:
            logger.debug("Could not load org memberships for WS client", exc_info=True)

    try:
        # Keep connection alive and listen for pings
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)

                # Handle ping/pong
                if data == "ping":
                    await websocket.send_text("pong")

            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _unregister_client(websocket)


# Helper functions for different event types
async def emit_task_progress(task_id: str, progress: dict):
    import logging
    logging.getLogger(__name__).info(f"[WebSocket] Emitting task:progress - taskId: {task_id}, percentage: {progress.get('percentage', 'N/A')}%")
    await broadcast_event("task:progress", {"taskId": task_id, **progress})


async def emit_task_error(task_id: str, error: str):
    import logging
    logging.getLogger(__name__).info(f"[WebSocket] Emitting task:error - taskId: {task_id}, error: {error[:100]}...")
    await broadcast_event("task:error", {"taskId": task_id, "error": error})


async def emit_task_status(task_id: str, status: str, review_reason: str | None = None):
    import logging
    payload = {"taskId": task_id, "status": status}
    if review_reason:
        payload["reviewReason"] = review_reason
        logging.getLogger(__name__).info(f"[WebSocket] Emitting task:status - taskId: {task_id}, status: {status}, reviewReason: {review_reason}")
    else:
        logging.getLogger(__name__).info(f"[WebSocket] Emitting task:status - taskId: {task_id}, status: {status}")
    await broadcast_event("task:status", payload)


async def emit_task_log(task_id: str, log: str):
    import logging
    # Only log the first 50 chars to avoid flooding logs with full log content
    log_preview = log[:50].replace('\n', '\\n') if len(log) > 50 else log.replace('\n', '\\n')
    logging.getLogger(__name__).debug(f"[WebSocket] Emitting task:log - taskId: {task_id}, log: {log_preview}...")
    await broadcast_event("task:log", {"taskId": task_id, "log": log})


async def emit_task_update(task_id: str, task_data: dict):
    """Emit task data update for frontend to refresh task card."""
    import logging
    exec_progress = task_data.get("executionProgress", {})
    phase = exec_progress.get("phase", "N/A") if exec_progress else "N/A"
    progress = exec_progress.get("phaseProgress", "N/A") if exec_progress else "N/A"
    logging.getLogger(__name__).info(f"[WebSocket] Emitting task:update - taskId: {task_id}, phase: {phase}, progress: {progress}%")
    await broadcast_event("task:update", {"taskId": task_id, **task_data})


async def emit_changelog_progress(project_id: str, progress: dict):
    await broadcast_event("changelog:progress", {"projectId": project_id, **progress})


async def emit_insights_chunk(project_id: str, chunk: str):
    await broadcast_event("insights:chunk", {"projectId": project_id, "chunk": chunk})


async def emit_insights_status(project_id: str, status: str):
    await broadcast_event("insights:status", {"projectId": project_id, "status": status})


async def emit_profile_switch(task_id: str, switch_data: dict):
    """Emit profile switch event for reactive failover."""
    import logging
    from_profile = switch_data.get("fromProfile", "N/A")
    to_profile = switch_data.get("toProfile", "N/A")
    logging.getLogger(__name__).info(f"[WebSocket] Emitting task:profile-switch - taskId: {task_id}, from: {from_profile}, to: {to_profile}")
    await broadcast_event("task:profile-switch", {"taskId": task_id, **switch_data})


async def emit_task_logs_stream(spec_id: str, chunk: dict):
    """Emit a task log chunk for real-time streaming to open task detail modals.

    This event streams individual log entries as they're added to task_logs.json,
    enabling live updates in the frontend without file polling.

    Args:
        spec_id: The spec/task identifier (e.g., "007-task-update-progress-logs")
        chunk: The log chunk dict matching TaskLogStreamChunk interface:
            - type: 'text' | 'tool_start' | 'tool_end' | 'phase_start' | 'phase_end' | 'error'
            - content: (optional) Log message content
            - phase: (optional) Current phase (planning, coding, validation)
            - timestamp: (optional) ISO timestamp
            - tool: (optional) { name: string, input?: string, success?: boolean }
            - subtask_id: (optional) Current subtask identifier
    """
    import logging
    chunk_type = chunk.get("type", "unknown")
    content_preview = chunk.get("content", "")[:50].replace('\n', '\\n') if chunk.get("content") else ""
    logging.getLogger(__name__).debug(
        f"[WebSocket] Emitting task-logs:stream - specId: {spec_id}, "
        f"type: {chunk_type}, content: {content_preview}..."
    )
    await broadcast_event("task-logs:stream", {"specId": spec_id, "chunk": chunk})


async def emit_project_usage(project_id: str, usage: dict):
    """Emit a token usage event scoped to a project (not tied to any spec).

    Used by in-process LLM features (Hermes, Insights chat) that record
    usage at the project root rather than under a spec dir. The dashboard
    rolls these into the same project totals as per-task agent usage.
    """
    import logging
    logging.getLogger(__name__).debug(
        f"[WebSocket] Emitting project:usage - projectId: {project_id}, "
        f"feature: {usage.get('feature')}, in: {usage.get('input_tokens')}, "
        f"out: {usage.get('output_tokens')}"
    )
    await broadcast_event("project:usage", {"projectId": project_id, "usage": usage})


async def emit_task_usage(task_id: str, usage: dict):
    """Emit a token usage event for a task.

    Fired each time the agent's SDK ResultMessage reports per-turn token counts.
    The frontend uses these to update per-task and per-project token totals live
    on the dashboard without re-fetching usage.json.
    """
    import logging
    logging.getLogger(__name__).debug(
        f"[WebSocket] Emitting task:usage - taskId: {task_id}, "
        f"phase: {usage.get('phase')}, in: {usage.get('input_tokens')}, "
        f"out: {usage.get('output_tokens')}"
    )
    await broadcast_event("task:usage", {"taskId": task_id, "usage": usage})


async def emit_preview_status(
    task_id: str,
    project_id: str | None,
    status: str,
    strategy: str,
    url: str | None = None,
    error: str | None = None,
):
    """Emit a preview lifecycle transition (building -> running/failed/stopped).

    Emitted by both the local preview service (dev-server / compose-local) and
    the remote docker preview worker so the UI updates immediately instead of
    waiting for its poll.
    """
    logger.info(
        "[WebSocket] Emitting preview:status - taskId: %s, status: %s, strategy: %s",
        task_id, status, strategy,
    )
    # TODO: scope preview:status to the task owner via send_to_user instead of
    # broadcasting to every connected client.
    await broadcast_event("preview:status", {
        "taskId": task_id,
        "projectId": project_id,
        "status": status,
        "strategy": strategy,
        "url": url,
        "error": error,
    })


async def emit_preview_log(task_id: str, line: str):
    """Emit a single line of preview build/run output for the live log panel."""
    # TODO: scope preview:log to the task owner via send_to_user instead of
    # broadcasting to every connected client.
    await broadcast_event("preview:log", {"taskId": task_id, "line": line})


async def emit_jenkins_status(
    task_id: str,
    project_id: str | None,
    status: str,
    build_url: str | None = None,
    error: str | None = None,
):
    """Emit a Jenkins deploy lifecycle transition (bumping -> publishing ->
    pushing -> triggering -> queued -> building -> success/failed)."""
    logger.info(
        "[WebSocket] Emitting jenkins:status - taskId: %s, status: %s", task_id, status,
    )
    await broadcast_event("jenkins:status", {
        "taskId": task_id,
        "projectId": project_id,
        "status": status,
        "buildUrl": build_url,
        "error": error,
    })


async def emit_jenkins_log(task_id: str, line: str):
    """Emit a single line of Jenkins-deploy output (gradle publish / push /
    trigger progress) for the live log panel."""
    await broadcast_event("jenkins:log", {"taskId": task_id, "line": line})


async def emit_subtask_update(task_id: str, subtask_id: str, status: str, previous_status: str | None = None):
    """Emit a subtask status change event for granular real-time updates.

    This event is emitted when an individual subtask's status changes, allowing
    the frontend to update subtask checkmarks in real-time without waiting for
    the full task update cycle.

    Args:
        task_id: The task/spec identifier
        subtask_id: The subtask identifier (e.g., "1.1", "2.3")
        status: The new status ("pending", "in_progress", "completed", "failed")
        previous_status: The previous status (optional, for logging/debugging)
    """
    import logging
    logger = logging.getLogger(__name__)
    if previous_status:
        logger.info(
            f"[WebSocket] Emitting task:subtask-update - taskId: {task_id}, "
            f"subtaskId: {subtask_id}, status: {previous_status} -> {status}"
        )
    else:
        logger.info(
            f"[WebSocket] Emitting task:subtask-update - taskId: {task_id}, "
            f"subtaskId: {subtask_id}, status: {status}"
        )
    await broadcast_event("task:subtask-update", {
        "taskId": task_id,
        "subtaskId": subtask_id,
        "status": status,
        "previousStatus": previous_status,
    })
