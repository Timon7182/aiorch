"""Hermes — a tiny LLM router for chat queries.

Picks a model per intent classification and (optionally) grounds the prompt
with hits from the project docs-index. Streams responses via httpx.AsyncClient.

This is intentionally smaller than MagesticAI's full agent provider stack —
chat is a separate concern from Planner/Coder/QA orchestration. Hermes routes
*conversational* queries to the best LLM; the agents run their own loop.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from . import docs_index_service

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Intent classification → Gemini model name. Policy in one place.
INTENT_MODEL: dict[str, str] = {
    "code": "gemini-2.5-pro",
    "plan": "gemini-2.5-pro",
    "docs": "gemini-2.5-flash",
    "qa": "gemini-2.5-flash",
    "chat": "gemini-2.5-flash",
}

_CODE_RE = re.compile(
    r"\b(write|implement|fix|refactor|debug|patch|function|class|api|sql|code|method)\b",
    re.IGNORECASE,
)
_PLAN_RE = re.compile(
    r"\b(plan|architecture|design|breakdown|steps|approach|roadmap|tradeoff)\b",
    re.IGNORECASE,
)
_DOCS_RE = re.compile(
    r"\b(doc|documentation|spec|readme|describe|explain|what is|how does|overview)\b",
    re.IGNORECASE,
)


def classify(query: str) -> str:
    if _CODE_RE.search(query):
        return "code"
    if _PLAN_RE.search(query):
        return "plan"
    if _DOCS_RE.search(query):
        return "docs"
    return "chat"


@dataclass
class HermesRoute:
    intent: str
    model: str
    citations: list[dict[str, Any]]


def route_and_augment(query: str, project: str | None) -> HermesRoute:
    intent = classify(query)
    model = INTENT_MODEL.get(intent, INTENT_MODEL["chat"])
    citations: list[dict[str, Any]] = []
    if project:
        try:
            citations = docs_index_service.search(project, query, limit=5)
        except Exception:
            citations = []
    return HermesRoute(intent=intent, model=model, citations=citations)


def _context_block(citations: list[dict[str, Any]]) -> str:
    if not citations:
        return ""
    lines: list[str] = ["", "Context from project docs (cite by [file:line]):"]
    for c in citations:
        path = c.get("file_path") or "unknown"
        line_start = c.get("line_start") or 0
        heading = c.get("heading") or ""
        snippet = c.get("snippet") or ""
        lines.append(f"[{path}:{line_start}] {heading}\n{snippet}")
        lines.append("---")
    return "\n".join(lines)


def _system_prompt(route: HermesRoute, project: str | None) -> str:
    return (
        "You are Hermes, an engineering assistant for the user's project workspace. "
        f"You have been routed to handle intent={route.intent!r} for the {project!r} "
        "project. Be concise, technically precise, and cite project sources inline "
        "as [path:line] when you use them."
    )


async def stream_chat(query: str, project: str | None) -> AsyncIterator[dict[str, Any]]:
    """Yield event payloads (dicts) describing the chat stream.

    Caller is responsible for SSE framing.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        yield {"type": "error", "value": "GEMINI_API_KEY not configured on the server"}
        return

    route = route_and_augment(query, project)
    yield {"type": "routing", "intent": route.intent, "model": route.model}
    if route.citations:
        yield {"type": "citations", "value": route.citations}

    prompt = query + _context_block(route.citations)

    url = (
        f"{GEMINI_BASE}/{route.model}:streamGenerateContent?alt=sse&key={api_key}"
    )
    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": _system_prompt(route, project)}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1500},
    }

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", errors="replace")
                    yield {
                        "type": "error",
                        "value": f"LLM HTTP {resp.status_code}: {detail[:400]}",
                    }
                    return
                async for raw in resp.aiter_lines():
                    if not raw:
                        continue
                    if not raw.startswith("data:"):
                        continue
                    payload = raw[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    try:
                        text = chunk["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError, TypeError):
                        text = ""
                    if text:
                        yield {"type": "token", "value": text}
    except Exception as exc:
        yield {"type": "error", "value": f"LLM call failed: {exc!r}"}
    yield {"type": "done"}
