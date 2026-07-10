"""
Generic OpenAI-compatible provider for insights chat.

Supports LM Studio, vLLM, LocalAI, Jan — any server exposing
POST /v1/chat/completions with SSE streaming.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from ...websockets.events import broadcast_event
from ..usage_recorder import record_project_usage
from .base import ProviderInfo, ProviderModel, ProviderStrategy

logger = logging.getLogger(__name__)

# Known OpenAI-compat providers with their default URLs
OPENAI_COMPAT_PROVIDERS = {
    "lmstudio": {
        "display_name": "LM Studio",
        "icon": "lmstudio",
        "base_url": "http://localhost:1234",
    },
    "localai": {
        "display_name": "LocalAI",
        "icon": "localai",
        "base_url": "http://localhost:8080",
    },
    "vllm": {
        "display_name": "vLLM",
        "icon": "vllm",
        "base_url": "http://localhost:8000",
    },
    "jan": {
        "display_name": "Jan",
        "icon": "jan",
        "base_url": "http://localhost:1337",
    },
}


class OpenAICompatProvider(ProviderStrategy):
    """Provider for OpenAI-compatible HTTP servers."""

    def __init__(self, provider_id: str, base_url: str | None = None) -> None:
        config = OPENAI_COMPAT_PROVIDERS.get(provider_id, {})
        self.provider_id = provider_id
        self.base_url = base_url or config.get("base_url", "http://localhost:8080")
        self.display_name = config.get("display_name", provider_id.title())
        self.icon = config.get("icon", provider_id)

    async def detect(self) -> ProviderInfo:
        info = ProviderInfo(
            provider=self.provider_id,
            available=False,
            display_name=self.display_name,
            icon=self.icon,
            auth_method=None,
            models=[],
        )

        try:
            import httpx
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.get(f"{self.base_url}/v1/models")
                resp.raise_for_status()
                data = resp.json()

                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    if model_id:
                        info.models.append(ProviderModel(
                            id=model_id,
                            label=model_id,
                        ))

                if info.models:
                    info.available = True
        except Exception as e:
            logger.debug(f"[OpenAICompat:{self.provider_id}] Detection failed: {e}")

        return info

    async def send_message(
        self,
        project_path: Path,
        project_id: str,
        message: str,
        model: str | None,
        model_config: dict | None,
        conversation_history: list[dict] | None,
        working_dir: Path | None = None,
        attachment_dir: Path | None = None,  # unused — attachments are inlined upstream
        session_id: str | None = None,
    ) -> str:
        # working_dir is accepted for interface parity; this provider is a plain
        # chat endpoint with no filesystem tool access, so branch selection has
        # no effect here.
        _ = working_dir
        effective_model = model or (model_config or {}).get("model", "")

        messages = []
        if conversation_history:
            for msg in conversation_history[-10:]:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })
        messages.append({"role": "user", "content": message})

        payload = {
            "model": effective_model,
            "messages": messages,
            "stream": True,
        }

        logger.info(f"[OpenAICompat:{self.provider_id}] Streaming: {effective_model}")

        try:
            import httpx

            await broadcast_event("insights:chunk", {
                "projectId": project_id, "sessionId": session_id,
                "type": "text",
                "content": "",
            })

            accumulated = ""
            stream_start = time.monotonic()
            # OpenAI-spec servers may send a final chunk with `stream_options:
            # {"include_usage": true}` opted-in containing real token totals.
            # Track the latest value we observe and record it after the stream
            # closes — fall back to estimates if the server never sends it.
            final_usage: dict | None = None

            # Ask the server to include usage on the final chunk. Harmless for
            # servers that don't support it (extra payload key is ignored).
            payload = {**payload, "stream_options": {"include_usage": True}}

            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    resp.raise_for_status()

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue

                        # SSE format: "data: {...}"
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                usage_obj = data.get("usage")
                                if isinstance(usage_obj, dict):
                                    final_usage = usage_obj
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    accumulated += content
                                    await broadcast_event("insights:chunk", {
                                        "projectId": project_id, "sessionId": session_id,
                                        "type": "text",
                                        "content": content,
                                    })
                            except (json.JSONDecodeError, IndexError):
                                continue

            elapsed = time.monotonic() - stream_start
            # Prefer real usage from the server's final chunk; fall back to a
            # crude 4-chars-per-token estimate for servers that don't supply it.
            if final_usage:
                output_tokens = int(final_usage.get("completion_tokens", 0) or 0)
                input_tokens = int(final_usage.get("prompt_tokens", 0) or 0)
                record_project_usage(
                    project_path=project_path,
                    project_id=project_id,
                    feature="insights",
                    phase="chat",
                    model=effective_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                reported_tokens = output_tokens or max(1, len(accumulated) // 4)
                estimated_flag = False
            else:
                reported_tokens = max(1, len(accumulated) // 4)
                estimated_flag = True

            tokens_per_sec = round(reported_tokens / elapsed, 1) if elapsed > 0 else 0

            await broadcast_event("insights:chunk", {
                "projectId": project_id, "sessionId": session_id,
                "type": "done",
                "metrics": {
                    "outputTokens": reported_tokens,
                    "tokensPerSecond": tokens_per_sec,
                    "elapsedSeconds": round(elapsed, 1),
                    "estimated": estimated_flag,
                },
            })

            return accumulated

        except Exception as e:
            logger.error(f"[OpenAICompat:{self.provider_id}] Error: {e}", exc_info=True)
            await broadcast_event("insights:chunk", {
                "projectId": project_id, "sessionId": session_id,
                "type": "error",
                "error": str(e),
            })
            return ""
