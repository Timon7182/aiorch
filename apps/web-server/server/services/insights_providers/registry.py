"""
Provider registry — singleton instances and concurrent detection.
"""

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from .base import ProviderInfo, ProviderStrategy
from .claude_provider import ClaudeProvider
from .codex_provider import CodexProvider
from .gemini_provider import GeminiProvider
from .ollama_provider import OllamaProvider
from .openai_compat_provider import OpenAICompatProvider

logger = logging.getLogger(__name__)

# Prefix marking a provider id that resolves to a user-defined llm_endpoints row
# (e.g. ``endpoint:<uuid>``) rather than a built-in provider singleton.
ENDPOINT_PREFIX = "endpoint:"

# Singleton provider instances
_providers: dict[str, ProviderStrategy] = {}


def _endpoints_db_path() -> Path:
    """Path to the web-server SQLite DB holding the ``llm_endpoints`` table."""
    try:
        from ...config import get_settings
        return Path(get_settings().PROJECTS_DATA_DIR) / "data.db"
    except Exception:
        return Path.home() / ".magestic-ai" / "data.db"


def _load_saved_endpoints() -> list[dict]:
    """Load user-defined OpenAI-compatible endpoints (LM Studio, vLLM, OpenRouter…).

    Returns a list of dicts with keys ``id, label, base_url, api_key,
    default_model, headers``. Reads the same ``llm_endpoints`` table the REST
    ``/llm-endpoints`` routes manage. Best-effort: any DB error yields ``[]``.
    """
    db_path = _endpoints_db_path()
    if not db_path.exists():
        return []
    rows: list[dict] = []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT id, label, base_url, api_key, default_model, headers_json "
                "FROM llm_endpoints ORDER BY created_at ASC"
            )
            for r in cur.fetchall():
                headers = None
                if r[5]:
                    try:
                        headers = json.loads(r[5])
                    except (json.JSONDecodeError, TypeError):
                        headers = None
                rows.append({
                    "id": r[0],
                    "label": r[1],
                    "base_url": r[2],
                    "api_key": r[3],
                    "default_model": r[4],
                    "headers": headers,
                })
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug(f"[Registry] Could not read llm_endpoints: {exc}")
    return rows


def _endpoint_provider(endpoint: dict) -> OpenAICompatProvider:
    """Build an OpenAI-compat provider for a saved endpoint row."""
    return OpenAICompatProvider(
        f"{ENDPOINT_PREFIX}{endpoint['id']}",
        base_url=endpoint["base_url"],
        api_key=endpoint["api_key"],
        display_name=endpoint["label"],
        icon="openai_compat",
        headers=endpoint["headers"],
        default_model=endpoint["default_model"],
    )


def _init_providers() -> None:
    """Lazily initialize all provider singletons."""
    global _providers
    if _providers:
        return

    _providers = {
        "claude": ClaudeProvider(),
        "codex": CodexProvider(),
        "gemini": GeminiProvider(),
        "ollama": OllamaProvider(),
        "lmstudio": OpenAICompatProvider("lmstudio"),
        "localai": OpenAICompatProvider("localai"),
        "vllm": OpenAICompatProvider("vllm"),
        "jan": OpenAICompatProvider("jan"),
    }


def get_provider(provider_id: str) -> ProviderStrategy:
    """Get a provider instance by ID. Defaults to Claude.

    ``endpoint:<id>`` resolves to a user-defined OpenAI-compatible endpoint,
    configured on the fly from its saved base_url/api_key.
    """
    _init_providers()

    if provider_id.startswith(ENDPOINT_PREFIX):
        endpoint_id = provider_id[len(ENDPOINT_PREFIX):]
        for endpoint in _load_saved_endpoints():
            if endpoint["id"] == endpoint_id:
                return _endpoint_provider(endpoint)
        logger.warning(
            f"Endpoint '{endpoint_id}' not found, falling back to Claude"
        )
        return _providers["claude"]

    provider = _providers.get(provider_id)
    if provider is None:
        logger.warning(f"Unknown provider '{provider_id}', falling back to Claude")
        provider = _providers["claude"]
    return provider


async def _timed_detect(
    provider_id: str, provider: ProviderStrategy
) -> tuple[str, float, ProviderInfo | Exception]:
    """Run a single provider's detect() and record elapsed time."""
    start = time.perf_counter()
    try:
        info = await provider.detect()
        elapsed = time.perf_counter() - start
        return provider_id, elapsed, info
    except Exception as exc:
        elapsed = time.perf_counter() - start
        return provider_id, elapsed, exc


async def detect_all_providers() -> list[ProviderInfo]:
    """Run detection for all providers concurrently."""
    _init_providers()

    total_start = time.perf_counter()

    # Built-in provider singletons + one dynamic provider per saved endpoint.
    detect_targets: dict[str, ProviderStrategy] = dict(_providers)
    for endpoint in _load_saved_endpoints():
        detect_targets[f"{ENDPOINT_PREFIX}{endpoint['id']}"] = _endpoint_provider(endpoint)

    timed_results = await asyncio.gather(
        *[
            _timed_detect(pid, prov)
            for pid, prov in detect_targets.items()
        ],
    )

    timings: dict[str, str] = {}
    infos: list[ProviderInfo] = []
    for provider_id, elapsed, result in timed_results:
        timings[provider_id] = f"{elapsed:.3f}s"
        if isinstance(result, Exception):
            logger.warning(f"Provider detection failed for {provider_id}: {result}")
            continue
        infos.append(result)

    total_elapsed = time.perf_counter() - total_start
    timing_details = ", ".join(f"{k}={v}" for k, v in timings.items())
    logger.info(
        f"[Registry] Provider detection completed in {total_elapsed:.2f}s "
        f"({timing_details})"
    )

    return infos
