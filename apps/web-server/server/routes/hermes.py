"""Hermes chat route: POST /api/ext/hermes/chat (SSE streaming)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..services import hermes_service

router = APIRouter()


class HermesChatRequest(BaseModel):
    query: str = Field(min_length=1)
    project: str | None = None


@router.post("/hermes/chat", tags=["Hermes"])
async def hermes_chat(req: HermesChatRequest) -> StreamingResponse:
    async def event_stream():
        async for event in hermes_service.stream_chat(req.query, req.project):
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/hermes/classify", tags=["Hermes"])
async def hermes_classify(req: HermesChatRequest) -> dict[str, Any]:
    """Non-streaming classification — useful for UI to show routing before send."""
    route = await hermes_service.route_and_augment(req.query, req.project)
    return {
        "intent": route.intent,
        "model": route.model,
        "citation_count": len(route.citations),
        "has_graph_context": bool(route.graph_context),
    }
