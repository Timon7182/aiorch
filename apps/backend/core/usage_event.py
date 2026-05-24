"""
Token usage event protocol + per-spec persistence.

Each agent SDK ResultMessage carries token counts (input/output/cache). We:
  1. Emit a structured `__USAGE_EVENT__:` line to stdout so the web server's
     agent_service can mirror counts in real time via WebSocket.
  2. Append the same event to `<spec_dir>/usage.json` so totals survive
     across process restarts and can be aggregated per-project later.

Companion to core/phase_event.py.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USAGE_MARKER_PREFIX = "__USAGE_EVENT__:"
_DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

# Approximate per-1M-token USD pricing used as a fallback when the SDK does
# not surface `total_cost_usd`. Numbers track Anthropic public list pricing
# at the time of writing; refine as pricing changes.
_MODEL_PRICING: dict[str, dict[str, float]] = {
    # claude-opus-4.x
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    # claude-sonnet-4.x
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    # claude-haiku-4.x
    "haiku": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
}

# Order matters: more specific first so "claude-opus-4-7" matches "opus".
_MODEL_FAMILY_MATCHERS: list[tuple[str, str]] = [
    ("opus", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
]


def estimate_cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> float:
    """Best-effort cost estimate from public Anthropic pricing.

    Used when the SDK does not report `total_cost_usd` (some providers don't).
    Returns 0.0 for unknown/non-Claude models — better to show 0 than a wrong
    number from another vendor.
    """
    if not model:
        return 0.0
    model_lc = model.lower()
    family: str | None = None
    for needle, fam in _MODEL_FAMILY_MATCHERS:
        if needle in model_lc:
            family = fam
            break
    if family is None:
        return 0.0
    p = _MODEL_PRICING[family]
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_read_input_tokens * p["cache_read"]
        + cache_creation_input_tokens * p["cache_write"]
    ) / 1_000_000


def emit_usage(
    phase: str,
    *,
    spec_dir: Path | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cost_usd: float | None = None,
    agent: str | None = None,
    subtask: str | None = None,
) -> dict[str, Any]:
    """Emit a usage event to stdout and (optionally) append to usage.json.

    `cost_usd=None` falls back to a pricing estimate. Returns the event dict.
    """
    if cost_usd is None:
        cost_usd = estimate_cost_usd(
            model,
            input_tokens,
            output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
        )

    event: dict[str, Any] = {
        "phase": phase,
        "model": model,
        "agent": agent,
        "subtask": subtask,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_read_input_tokens": int(cache_read_input_tokens or 0),
        "cache_creation_input_tokens": int(cache_creation_input_tokens or 0),
        "cost_usd": float(cost_usd or 0.0),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        print(f"{USAGE_MARKER_PREFIX}{json.dumps(event, default=str)}", flush=True)
    except (OSError, UnicodeEncodeError) as e:
        if _DEBUG:
            print(f"[usage_event] emit failed: {e}", file=sys.stderr, flush=True)

    if spec_dir is not None:
        try:
            persist_usage(spec_dir, event)
        except Exception as e:
            if _DEBUG:
                print(f"[usage_event] persist failed: {e}", file=sys.stderr, flush=True)

    return event


def _empty_totals() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
    }


def _read_usage(usage_file: Path) -> dict[str, Any]:
    if usage_file.exists():
        try:
            data = json.loads(usage_file.read_text())
            data.setdefault("totals", _empty_totals())
            data.setdefault("by_phase", {})
            data.setdefault("by_model", {})
            data.setdefault("events", [])
            return data
        except (OSError, json.JSONDecodeError):
            pass
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


def persist_usage(spec_dir: Path, event: dict[str, Any]) -> None:
    """Append `event` to `<spec_dir>/usage.json` and update totals/breakdowns."""
    spec_dir.mkdir(parents=True, exist_ok=True)
    usage_file = spec_dir / "usage.json"
    data = _read_usage(usage_file)

    _accumulate(data["totals"], event)

    phase_key = event.get("phase") or "unknown"
    phase_entry = data["by_phase"].setdefault(phase_key, _empty_totals())
    _accumulate(phase_entry, event)
    if event.get("model"):
        phase_entry["model"] = event["model"]

    model_key = event.get("model") or "unknown"
    model_entry = data["by_model"].setdefault(model_key, _empty_totals())
    _accumulate(model_entry, event)

    events = data["events"]
    events.append(event)
    # Cap to keep the file bounded; aggregates already hold history.
    if len(events) > 2000:
        data["events"] = events[-2000:]

    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp = usage_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(usage_file)
