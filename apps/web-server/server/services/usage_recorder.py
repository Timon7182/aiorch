"""
Project-level token usage recorder for in-process features (Hermes, Insights).

The agent runtime persists usage via `apps/backend/core/usage_event.py` to
per-spec `usage.json` files. That path is for subprocess runs. Features that
make LLM calls directly inside the web server (chat, Hermes routing) live
outside any spec, so we persist them at the project root under:

    {project_path}/.magestic-ai/usage/{feature}.json

Same JSON shape as the per-spec file, so the project-level rollup endpoint
can sum both without special-casing. Also broadcasts a `project:usage`
WebSocket event so the dashboard updates live.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Anthropic public pricing per 1M tokens — copy of the table in
# core/usage_event.py. Kept in sync manually; not worth a shared package for
# six numbers.
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "haiku": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # Gemini 2.5 list pricing (per 1M, text). Cache pricing approximated.
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0, "cache_read": 0.31, "cache_write": 0.0},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.075, "cache_write": 0.0},
}


def _match_pricing(model: str | None) -> dict[str, float] | None:
    if not model:
        return None
    m = model.lower()
    if "opus" in m:
        return _MODEL_PRICING["opus"]
    if "sonnet" in m:
        return _MODEL_PRICING["sonnet"]
    if "haiku" in m:
        return _MODEL_PRICING["haiku"]
    if "gemini" in m and "pro" in m:
        return _MODEL_PRICING["gemini-2.5-pro"]
    if "gemini" in m and "flash" in m:
        return _MODEL_PRICING["gemini-2.5-flash"]
    return None


def estimate_cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> float:
    p = _match_pricing(model)
    if p is None:
        return 0.0
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_read_input_tokens * p["cache_read"]
        + cache_creation_input_tokens * p["cache_write"]
    ) / 1_000_000


def _empty_totals() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
    }


def _read(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data.setdefault("totals", _empty_totals())
            data.setdefault("by_phase", {})
            data.setdefault("by_model", {})
            data.setdefault("events", [])
            return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[usage_recorder] failed to read {path}: {exc}")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "created_at": now,
        "updated_at": now,
        "totals": _empty_totals(),
        "by_phase": {},
        "by_model": {},
        "events": [],
    }


def _accumulate(target: dict[str, Any], event: dict[str, Any]) -> None:
    for k in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        target[k] = int(target.get(k, 0)) + int(event.get(k, 0) or 0)
    target["cost_usd"] = float(target.get("cost_usd", 0.0)) + float(
        event.get("cost_usd", 0.0) or 0.0
    )
    target["calls"] = int(target.get("calls", 0)) + 1


def record_project_usage(
    *,
    project_path: Path,
    feature: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cost_usd: float | None = None,
    phase: str | None = None,
    project_id: str | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append an LLM call to `{project_path}/.magestic-ai/usage/{feature}.json`.

    Best-effort: any IO or schema error is logged and swallowed — billing
    accuracy is nice to have, but should never break a chat response.
    Returns the event dict on success so callers can attach it to logs.
    """
    if input_tokens <= 0 and output_tokens <= 0:
        return None  # Nothing real to record (avoid noise from estimate-only paths).

    if cost_usd is None:
        cost_usd = estimate_cost_usd(
            model,
            input_tokens,
            output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
        )

    event: dict[str, Any] = {
        "phase": phase or feature,
        "feature": feature,
        "model": model,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_read_input_tokens": int(cache_read_input_tokens),
        "cache_creation_input_tokens": int(cache_creation_input_tokens),
        "cost_usd": float(cost_usd),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extras:
        event.update(extras)

    try:
        usage_dir = project_path / ".magestic-ai" / "usage"
        usage_dir.mkdir(parents=True, exist_ok=True)
        path = usage_dir / f"{feature}.json"
        data = _read(path)

        _accumulate(data["totals"], event)

        phase_key = event["phase"]
        phase_entry = data["by_phase"].setdefault(phase_key, _empty_totals())
        _accumulate(phase_entry, event)
        if event["model"]:
            phase_entry["model"] = event["model"]

        model_key = event.get("model") or "unknown"
        model_entry = data["by_model"].setdefault(model_key, _empty_totals())
        _accumulate(model_entry, event)

        events = data["events"]
        events.append(event)
        if len(events) > 2000:
            data["events"] = events[-2000:]

        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        data["feature"] = feature

        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    except Exception as exc:
        logger.warning(f"[usage_recorder] persist failed for {feature}: {exc}")
        return event

    # Broadcast over the global events WebSocket so the dashboard updates
    # without polling. Fire-and-forget — we never block the LLM response.
    if project_id:
        try:
            from ..websockets.events import emit_project_usage

            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(emit_project_usage(project_id, event))
        except Exception as exc:
            logger.debug(f"[usage_recorder] WS emit failed: {exc}")

    return event
