"""
Graylog log reader.

Fetches the full log entry behind the short alert excerpts that land in
Telegram/notification channels. Alerts typically carry a Graylog search link
like ``http://graylog:9000/search?q=_id:<uuid>&...`` — we extract the message
``_id`` and pull the complete document (including ``full_message`` with the
stack trace) over the Graylog REST API using basic auth.

Credentials are per-project, stored in ``<project>/.magestic-ai/.env`` as
``GRAYLOG_URL`` / ``GRAYLOG_USERNAME`` / ``GRAYLOG_PASSWORD`` and edited from
the project settings UI (Telegram & Graylog section).
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Graylog message ids are UUIDs; alerts embed them as `_id:<uuid>` (raw or
# URL-encoded `_id%3A<uuid>` inside the search link).
_MESSAGE_ID_RE = re.compile(
    r"_id(?::|%3[Aa])\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# Fields that carry the actual log payload, rendered first and in this order.
_PRIMARY_FIELDS = ("timestamp", "source", "level", "message", "full_message")

# Noise fields not worth showing the model.
_SKIP_FIELDS = {"streams", "gl2_accounted_message_size", "gl2_message_id",
                "gl2_remote_ip", "gl2_remote_port", "gl2_source_input",
                "gl2_source_node", "_index"}

MAX_LOG_CHARS = 20_000


@dataclass
class GraylogConfig:
    url: str
    username: str = ""
    password: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())


def extract_message_ids(text: str) -> list[str]:
    """Extract Graylog message ``_id`` values referenced in ``text``."""
    if not text:
        return []
    seen: list[str] = []
    for match in _MESSAGE_ID_RE.finditer(text):
        mid = match.group(1).lower()
        if mid not in seen:
            seen.append(mid)
    return seen


async def fetch_log_message(
    config: GraylogConfig, message_id: str, client: httpx.AsyncClient
) -> dict | None:
    """Fetch a single log document by ``_id`` via the Graylog search API.

    Returns the message's field dict, or ``None`` when the message can't be
    found / the API call fails (callers degrade to the alert excerpt).
    """
    base = config.url.strip().rstrip("/")
    if not base:
        return None
    try:
        resp = await client.get(
            f"{base}/api/search/universal/relative",
            params={
                "query": f"_id:{message_id}",
                # range=0 => unrestricted time range; the alert may be old.
                "range": 0,
                "limit": 1,
            },
            headers={
                "Accept": "application/json",
                "X-Requested-By": "MagesticAI",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"[Graylog] Failed to fetch message {message_id}: {e}")
        return None

    messages = data.get("messages") or []
    if not messages:
        logger.info(f"[Graylog] No message found for _id:{message_id}")
        return None
    return messages[0].get("message") or None


def format_log_for_prompt(message_id: str, fields: dict) -> str:
    """Render a fetched log document as a readable prompt block."""
    lines = [f"Graylog message _id:{message_id}"]
    for key in _PRIMARY_FIELDS:
        value = fields.get(key)
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
    extras = {
        k: v for k, v in sorted(fields.items())
        if k not in _PRIMARY_FIELDS and k not in _SKIP_FIELDS
        and not k.startswith("gl2_") and v not in (None, "")
    }
    if extras:
        lines.append("other fields:")
        for k, v in extras.items():
            lines.append(f"  {k}: {v}")
    text = "\n".join(str(line) for line in lines)
    if len(text) > MAX_LOG_CHARS:
        text = text[:MAX_LOG_CHARS] + "\n... [truncated]"
    return text


async def resolve_logs_from_text(config: GraylogConfig, *texts: str) -> str:
    """Find Graylog ``_id`` references in the given texts and fetch each log.

    Returns a prompt-ready block (empty string when nothing was found or
    Graylog isn't configured).
    """
    if not config.configured:
        return ""
    ids: list[str] = []
    for text in texts:
        for mid in extract_message_ids(text or ""):
            if mid not in ids:
                ids.append(mid)
    if not ids:
        return ""

    ids = ids[:3]  # cap: alerts normally reference a single message
    async with httpx.AsyncClient(
        auth=(config.username, config.password) if config.username else None,
        timeout=20.0,
    ) as client:
        results = await asyncio.gather(
            *(fetch_log_message(config, mid, client) for mid in ids)
        )

    blocks: list[str] = []
    for mid, fields in zip(ids, results):
        if fields:
            blocks.append(format_log_for_prompt(mid, fields))
        else:
            blocks.append(
                f"Graylog message _id:{mid} — could not be fetched "
                f"(check Graylog availability/credentials in project settings)."
            )
    return "\n\n".join(blocks)
