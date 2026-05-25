"""Hermes — a tiny LLM router for chat queries.

Picks a model per intent classification and (optionally) grounds the prompt
with hits from the project docs-index. Streams responses via httpx.AsyncClient.

This is intentionally smaller than MagesticAI's full agent provider stack —
chat is a separate concern from Planner/Coder/QA orchestration. Hermes routes
*conversational* queries to the best LLM; the agents run their own loop.

Grounding stack (in priority order, run in parallel):
  1. Graphify graph traversal via `graphify query` if the project has a
     graphify-out/graph.json. This is the structural / cross-cutting layer
     (good for "where does X get used?" questions).
  2. FTS5 over markdown docs via docs_index_service. This is the snippet
     layer (good for "what does the auth doc say about token refresh?").

Both feed into the same prompt; the graph block + citations both appear in
the LLM's context with clear provenance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import docs_index_service
from .project_resolve import (
    resolve_project_dir as _resolve_project_path,
    resolve_project_info as _resolve_project_info,
)
from .usage_recorder import record_project_usage

logger = logging.getLogger(__name__)

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
    graph_context: str = field(default="")


def _resolve_graphify_bin() -> str | None:
    """Find the graphify CLI in the venv first, then PATH."""
    venv_bin = Path(sys.executable).parent / "graphify"
    if venv_bin.exists():
        return str(venv_bin)
    return shutil.which("graphify")


async def _graphify_query(project_path: Path, query: str) -> str:
    """Run `graphify query` against the project's graph and capture stdout.

    Returns "" if graphify is unavailable, the project has no graph yet, the
    subprocess fails, or the lookup exceeds the 30s budget. Hermes degrades
    gracefully to FTS5-only grounding in any of those cases.
    """
    graph_file = project_path / "graphify-out" / "graph.json"
    if not graph_file.is_file():
        return ""

    bin_path = _resolve_graphify_bin()
    if bin_path is None:
        return ""

    # Keep OAuth env intact: `graphify query` may need to call the LLM via
    # the `claude` CLI (the same auth path docs_generator_service uses for
    # extraction), and the CLI reads CLAUDE_CODE_OAUTH_TOKEN + the
    # persisted credentials in ~/.claude/.
    env = os.environ.copy()

    cmd = [bin_path, "query", query, "--graph", str(graph_file), "--budget", "1500"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(project_path),
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        logger.info("[hermes] graphify query timed out; falling back to FTS only")
        return ""
    except OSError as e:
        logger.info(f"[hermes] graphify spawn failed: {e}; falling back to FTS only")
        return ""

    if proc.returncode != 0:
        logger.info(
            f"[hermes] graphify query exited {proc.returncode}; falling back to FTS only"
        )
        return ""

    text = (stdout or b"").decode("utf-8", "replace").strip()
    # Cap so a verbose graph traversal can't blow Gemini's context window.
    return text[:4000]


async def route_and_augment(query: str, project: str | None) -> HermesRoute:
    """Classify intent, pick model, and gather grounding (graph + FTS) in parallel."""
    intent = classify(query)
    model = INTENT_MODEL.get(intent, INTENT_MODEL["chat"])

    citations: list[dict[str, Any]] = []
    graph_context: str = ""

    if project:
        project_path = _resolve_project_path(project)

        async def _fts() -> list[dict[str, Any]]:
            try:
                return docs_index_service.search(project, query, limit=5)
            except Exception:
                return []

        async def _graph() -> str:
            if project_path is None:
                return ""
            return await _graphify_query(project_path, query)

        # Run both lookups concurrently; whichever finishes first doesn't
        # matter — we wait for both before returning the route.
        citations, graph_context = await asyncio.gather(_fts(), _graph())

    return HermesRoute(
        intent=intent,
        model=model,
        citations=citations,
        graph_context=graph_context,
    )


def _context_block(citations: list[dict[str, Any]], graph_context: str = "") -> str:
    blocks: list[str] = []

    if graph_context:
        blocks.append(
            "Context from project knowledge graph (graphify):\n" + graph_context
        )

    if citations:
        lines: list[str] = ["Context from project docs (cite by [file:line]):"]
        for c in citations:
            path = c.get("file_path") or "unknown"
            line_start = c.get("line_start") or 0
            heading = c.get("heading") or ""
            snippet = c.get("snippet") or ""
            lines.append(f"[{path}:{line_start}] {heading}\n{snippet}")
            lines.append("---")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return "\n\n" + "\n\n".join(blocks)


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

    route = await route_and_augment(query, project)
    yield {"type": "routing", "intent": route.intent, "model": route.model}
    if route.graph_context:
        yield {"type": "graph_context", "value": route.graph_context}
    if route.citations:
        yield {"type": "citations", "value": route.citations}

    prompt = query + _context_block(route.citations, route.graph_context)

    url = (
        f"{GEMINI_BASE}/{route.model}:streamGenerateContent?alt=sse&key={api_key}"
    )
    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": _system_prompt(route, project)}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1500},
    }

    # Gemini sends `usageMetadata` on the final SSE chunk with cumulative
    # totals — track the latest values we've seen so we can record them
    # exactly once after the stream closes.
    final_usage: dict[str, Any] | None = None

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
                    # Gemini's usageMetadata holds running totals; the final
                    # chunk has the canonical count. Always overwrite.
                    usage_md = chunk.get("usageMetadata")
                    if isinstance(usage_md, dict):
                        final_usage = usage_md
                    try:
                        text = chunk["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError, TypeError):
                        text = ""
                    if text:
                        yield {"type": "token", "value": text}
    except Exception as exc:
        yield {"type": "error", "value": f"LLM call failed: {exc!r}"}

    # Record real token usage to .magestic-ai/usage/hermes.json so the
    # dashboard can roll it into the project total. Anything that fails here
    # is swallowed inside record_project_usage — never break the chat reply.
    if final_usage and project:
        info = _resolve_project_info(project)
        if info:
            project_id, project_path = info
            record_project_usage(
                project_path=project_path,
                project_id=project_id,
                feature="hermes",
                phase=route.intent,
                model=route.model,
                input_tokens=int(final_usage.get("promptTokenCount", 0) or 0),
                output_tokens=int(final_usage.get("candidatesTokenCount", 0) or 0),
                cache_read_input_tokens=int(
                    final_usage.get("cachedContentTokenCount", 0) or 0
                ),
            )

    yield {"type": "done"}
