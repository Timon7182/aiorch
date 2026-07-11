"""
Telegram bot bridge for the insights chat.

Each project can be bound to a Telegram group chat from the dashboard
(project settings → Telegram & Graylog). When someone in that chat mentions
the bot — typically replying to an ETL error alert — the bot:

1. Resolves the project from the chat id,
2. Pulls the full log from Graylog when the alert references a message
   ``_id`` (see ``graylog_service``),
3. Runs an insights-chat turn (the conversation shows up in the project's
   chat history in the web UI, like any other session),
4. Replies in the Telegram thread with the assistant's answer.

Replies to the bot's own messages continue the same insights session, so a
back-and-forth in Telegram is one coherent chat thread in the UI.

Per-project config lives in ``<project>/.magestic-ai/.env``:
    TELEGRAM_ENABLED=true
    TELEGRAM_BOT_TOKEN=<bot token>
    TELEGRAM_CHAT_ID=<chat id>[,<chat id>...]
    GRAYLOG_URL=http://graylog:9000
    GRAYLOG_USERNAME=...
    GRAYLOG_PASSWORD=...

The service long-polls ``getUpdates`` (one poller per distinct bot token) and
re-reads project config every supervisor cycle, so dashboard changes take
effect without a server restart.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..paths import get_data_file
from . import graylog_service
from .graylog_service import GraylogConfig

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
POLL_TIMEOUT = 25          # getUpdates long-poll seconds
SUPERVISOR_INTERVAL = 20   # config re-scan seconds
MAX_REPLY_CHUNK = 3900     # Telegram hard limit is 4096
MAX_THREAD_KEYS = 1000     # cap for the thread → session map

STATE_FILE = "telegram_bot_state.json"


ACK_TEXT = "🔎 Проверяю…"


def _mention_re(bot_username: str) -> re.Pattern:
    """Case-insensitive @mention matcher with a trailing username boundary."""
    return re.compile(rf"@{re.escape(bot_username)}(?![A-Za-z0-9_])", re.IGNORECASE)


def _chat_id_variants(chat_id: str) -> set[str]:
    """Both spellings of a group chat id.

    Supergroup ids carry a ``-100`` prefix (``-1003192691355``) while users
    often copy the short form (``-3192691355``) from other tooling. Accept
    either in the settings by binding both.
    """
    variants = {chat_id}
    if chat_id.startswith("-100") and len(chat_id) > 4:
        variants.add("-" + chat_id[4:])
    elif chat_id.startswith("-") and not chat_id.startswith("-100"):
        variants.add("-100" + chat_id[1:])
    return variants


@dataclass
class ProjectBinding:
    project_id: str
    project_path: Path
    chat_ids: set[str]
    graylog: GraylogConfig


@dataclass
class BotConfig:
    token: str
    # chat_id -> binding (first project claiming a chat id wins)
    bindings: dict[str, ProjectBinding] = field(default_factory=dict)


def _read_project_env(project_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = project_path / ".magestic-ai" / ".env"
    if not env_path.exists():
        return env
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                # Same quote handling as the settings reader (routes/context.py)
                # so hand-quoted values behave identically in UI and bot.
                env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as e:
        logger.warning(f"[Telegram] Cannot read env for {project_path}: {e}")
    return env


def collect_bot_configs() -> dict[str, BotConfig]:
    """Scan registered projects for Telegram bindings. token -> BotConfig."""
    from ..routes.projects import load_projects

    configs: dict[str, BotConfig] = {}
    try:
        projects = load_projects()
    except Exception as e:
        logger.warning(f"[Telegram] Cannot load projects: {e}")
        return configs

    for project_id, data in projects.items():
        path = data.get("path")
        if not path:
            continue
        project_path = Path(path)
        env = _read_project_env(project_path)
        if env.get("TELEGRAM_ENABLED", "").lower() != "true":
            continue
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_ids = {
            c.strip() for c in env.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()
        }
        if not token or not chat_ids:
            continue

        binding = ProjectBinding(
            project_id=project_id,
            project_path=project_path,
            chat_ids=chat_ids,
            graylog=GraylogConfig(
                url=env.get("GRAYLOG_URL", ""),
                username=env.get("GRAYLOG_USERNAME", ""),
                password=env.get("GRAYLOG_PASSWORD", ""),
            ),
        )
        bot = configs.setdefault(token, BotConfig(token=token))
        for chat_id in chat_ids:
            for variant in _chat_id_variants(chat_id):
                bot.bindings.setdefault(variant, binding)

    return configs


class TelegramBotService:
    """Supervises one long-poll loop per configured bot token."""

    def __init__(self) -> None:
        self._supervisor_task: asyncio.Task | None = None
        self._pollers: dict[str, asyncio.Task] = {}   # token -> poll task
        self._configs: dict[str, BotConfig] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._handler_tasks: set[asyncio.Task] = set()
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._supervisor_task and not self._supervisor_task.done():
            return
        self._supervisor_task = asyncio.create_task(self._supervise())
        logger.info("[Telegram] Bot service started")

    async def stop(self) -> None:
        tasks = [
            t for t in [self._supervisor_task, *self._pollers.values(), *self._handler_tasks]
            if t and not t.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            # Wait for cancellation to land before snapshotting state, so a
            # mid-turn handler can't race (or outlive) the final save.
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pollers.clear()
        self._supervisor_task = None
        self._save_state()
        logger.info("[Telegram] Bot service stopped")

    async def _supervise(self) -> None:
        """Re-scan project config and reconcile poll loops."""
        while True:
            try:
                self._configs = await asyncio.to_thread(collect_bot_configs)
                # Start pollers for new tokens
                for token in self._configs:
                    task = self._pollers.get(token)
                    if task is None or task.done():
                        self._pollers[token] = asyncio.create_task(self._poll_bot(token))
                # Stop pollers for removed tokens
                for token in list(self._pollers):
                    if token not in self._configs:
                        self._pollers.pop(token).cancel()
                        logger.info("[Telegram] Stopped poller (binding removed)")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[Telegram] Supervisor cycle failed", exc_info=True)
            await asyncio.sleep(SUPERVISOR_INTERVAL)

    # ------------------------------------------------------------------
    # State (offsets + thread → session map)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            state_file = get_data_file(STATE_FILE)
            if state_file.exists():
                return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Telegram] Cannot load state: {e}")
        return {"offsets": {}, "threads": {}}

    def _save_state(self) -> None:
        try:
            threads = self._state.get("threads", {})
            if len(threads) > MAX_THREAD_KEYS:
                # Drop least-recently-used entries (_remember_thread re-inserts
                # touched keys at the end, so insertion order == recency).
                for key in list(threads)[: len(threads) - MAX_THREAD_KEYS]:
                    del threads[key]
                # Locks for sessions no longer referenced by any thread (and
                # not currently held) can go too.
                live = set(threads.values())
                self._session_locks = {
                    sid: lock for sid, lock in self._session_locks.items()
                    if sid in live or lock.locked()
                }
            get_data_file(STATE_FILE).write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[Telegram] Cannot save state: {e}")

    async def _save_state_async(self) -> None:
        """Persist state off the event loop (the file can reach ~100KB)."""
        await asyncio.to_thread(self._save_state)

    def _thread_key(self, chat_id: str, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    def _session_for_thread(self, chat_id: str, message_id: int | None) -> str | None:
        if message_id is None:
            return None
        return self._state.get("threads", {}).get(self._thread_key(chat_id, message_id))

    def _remember_thread(self, chat_id: str, message_id: int | None, session_id: str) -> None:
        if message_id is None:
            return
        threads = self._state.setdefault("threads", {})
        key = self._thread_key(chat_id, message_id)
        # Re-insert so active threads move to the end and survive LRU eviction.
        threads.pop(key, None)
        threads[key] = session_id

    # ------------------------------------------------------------------
    # Telegram API helpers
    # ------------------------------------------------------------------

    async def _api(self, client: httpx.AsyncClient, token: str, method: str, **params):
        resp = await client.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    async def _send_reply(
        self, client: httpx.AsyncClient, token: str, chat_id: str,
        reply_to: int | None, text: str,
    ) -> list[int]:
        """Send ``text`` (chunked) as a reply. Returns sent message ids."""
        sent_ids: list[int] = []
        chunks = [text[i: i + MAX_REPLY_CHUNK] for i in range(0, len(text), MAX_REPLY_CHUNK)] or [""]
        for idx, chunk in enumerate(chunks):
            params: dict = {"chat_id": chat_id, "text": chunk}
            if reply_to and idx == 0:
                params["reply_to_message_id"] = reply_to
                params["allow_sending_without_reply"] = True
            try:
                result = await self._api(client, token, "sendMessage", **params)
                sent_ids.append(result["message_id"])
            except Exception as e:
                logger.error(f"[Telegram] sendMessage failed: {e}")
                break
        return sent_ids

    async def _react(
        self, client: httpx.AsyncClient, token: str, chat_id: str, message_id: int | None
    ) -> None:
        """Best-effort 👀 reaction on the user's message (instant feedback)."""
        if message_id is None:
            return
        try:
            await self._api(
                client, token, "setMessageReaction",
                chat_id=chat_id, message_id=message_id,
                reaction=[{"type": "emoji", "emoji": "👀"}],
            )
        except Exception as e:
            logger.info(f"[Telegram] setMessageReaction skipped: {e}")

    async def _deliver_answer(
        self, client: httpx.AsyncClient, token: str, chat_id: str,
        reply_to: int | None, ack_id: int | None, text: str,
    ) -> list[int]:
        """Deliver the final answer, editing the ack message in place.

        The first chunk replaces the "🔎 Проверяю…" ack (editMessageText); any
        remaining chunks follow as new messages. Falls back to plain replies
        when there is no ack or the edit fails. Returns the answer message ids.
        """
        chunks = [text[i: i + MAX_REPLY_CHUNK] for i in range(0, len(text), MAX_REPLY_CHUNK)] or [""]
        sent_ids: list[int] = []
        rest = chunks
        if ack_id is not None:
            try:
                await self._api(
                    client, token, "editMessageText",
                    chat_id=chat_id, message_id=ack_id, text=chunks[0],
                )
                sent_ids.append(ack_id)
                rest = chunks[1:]
            except Exception as e:
                logger.warning(f"[Telegram] editMessageText failed, sending anew: {e}")
        if rest:
            for idx, chunk in enumerate(rest):
                params: dict = {"chat_id": chat_id, "text": chunk}
                if not sent_ids and idx == 0 and reply_to:
                    params["reply_to_message_id"] = reply_to
                    params["allow_sending_without_reply"] = True
                try:
                    result = await self._api(client, token, "sendMessage", **params)
                    sent_ids.append(result["message_id"])
                except Exception as e:
                    logger.error(f"[Telegram] sendMessage failed: {e}")
                    break
        return sent_ids

    async def _typing_loop(self, client: httpx.AsyncClient, token: str, chat_id: str) -> None:
        """Keep the 'typing…' indicator alive while a turn runs."""
        try:
            while True:
                try:
                    await self._api(client, token, "sendChatAction",
                                    chat_id=chat_id, action="typing")
                except Exception:
                    pass
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_bot(self, token: str) -> None:
        bot_id = token.split(":", 1)[0]
        async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 15) as client:
            # Resolve the bot's username for mention detection.
            bot_username = ""
            while not bot_username:
                try:
                    me = await self._api(client, token, "getMe")
                    bot_username = (me.get("username") or "").lower()
                    logger.info(f"[Telegram] Polling as @{bot_username}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[Telegram] getMe failed (retrying in 30s): {e}")
                    await asyncio.sleep(30)

            stored = self._state.get("offsets", {}).get(bot_id)
            offset = int(stored) if stored is not None else 0
            while stored is None:
                # First run for this bot: skip the pending backlog so we don't
                # reply to mentions that happened before the binding existed.
                # Must succeed before polling starts, else offset=0 would
                # replay (and answer) the entire pending history.
                try:
                    pending = await self._api(
                        client, token, "getUpdates", offset=-1, timeout=0
                    )
                    if pending:
                        offset = pending[-1]["update_id"] + 1
                    stored = offset
                    self._state.setdefault("offsets", {})[bot_id] = offset
                    await self._save_state_async()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[Telegram] Backlog skip failed (retrying in 30s): {e}")
                    await asyncio.sleep(30)

            while True:
                try:
                    updates = await self._api(
                        client, token, "getUpdates",
                        offset=offset, timeout=POLL_TIMEOUT,
                        allowed_updates=["message"],
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[Telegram] getUpdates failed: {e}")
                    await asyncio.sleep(10)
                    continue

                for update in updates:
                    offset = max(offset, update["update_id"] + 1)
                    self._state.setdefault("offsets", {})[bot_id] = offset
                    message = update.get("message")
                    if message:
                        self._dispatch(token, bot_id, bot_username, message)
                if updates:
                    await self._save_state_async()

    def _dispatch(self, token: str, bot_id: str, bot_username: str, message: dict) -> None:
        """Decide whether a message addresses the bot; spawn a handler if so."""
        chat_id = str(message.get("chat", {}).get("id", ""))
        config = self._configs.get(token)
        binding = config.bindings.get(chat_id) if config else None
        if binding is None:
            return
        sender = message.get("from", {})
        if sender.get("is_bot"):
            return

        # Note: entity offsets are UTF-16 code units (emoji shift them vs
        # Python indices), so a case-insensitive regex is the robust way to
        # detect the mention. The trailing boundary keeps @etl_bot from
        # matching inside @etl_bot_test (usernames are [A-Za-z0-9_]).
        text = message.get("text") or message.get("caption") or ""
        mentioned = bool(_mention_re(bot_username).search(text))

        reply_to = message.get("reply_to_message") or {}
        replying_to_bot = str(reply_to.get("from", {}).get("id", "")) == bot_id

        if not mentioned and not replying_to_bot:
            return

        task = asyncio.create_task(
            self._handle_request(token, bot_username, binding, message)
        )
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def _build_prompt(
        self, message: dict, ask: str, replied_text: str, graylog_block: str
    ) -> str:
        sender = message.get("from", {})
        sender_name = " ".join(
            p for p in [sender.get("first_name"), sender.get("last_name")] if p
        ) or sender.get("username") or "user"
        chat_title = message.get("chat", {}).get("title") or "Telegram"

        parts = [
            f'[Telegram] Message from "{sender_name}" in chat "{chat_title}".',
        ]
        if replied_text:
            parts.append(
                "The user replied to this alert/message:\n"
                "--- replied-to message ---\n"
                f"{replied_text}\n"
                "--- end replied-to message ---"
            )
        if graylog_block:
            parts.append(
                "Full log entry fetched from Graylog:\n"
                "--- graylog log ---\n"
                f"{graylog_block}\n"
                "--- end graylog log ---"
            )
        parts.append(f"User request:\n{ask}" if ask else
                     "User request: (no explicit question — analyze the error above, "
                     "find the likely root cause in the project code, and explain it)")
        parts.append(
            "Answer in the same language as the user's request. Your answer is sent "
            "back to the Telegram chat as plain text — keep it focused and readable, "
            "no heavy markdown formatting."
        )
        return "\n\n".join(parts)

    async def _handle_request(
        self, token: str, bot_username: str, binding: ProjectBinding, message: dict
    ) -> None:
        from .insights_service import get_insights_service

        chat_id = str(message["chat"]["id"])
        message_id = message.get("message_id")
        text = message.get("text") or message.get("caption") or ""
        ask = _mention_re(bot_username).sub("", text).strip()

        reply_to = message.get("reply_to_message") or {}
        reply_to_id = reply_to.get("message_id")
        replied_text = reply_to.get("text") or reply_to.get("caption") or ""

        service = get_insights_service()

        # Continue an existing thread session, else start a fresh one.
        session_id = (
            self._session_for_thread(chat_id, reply_to_id)
            or self._session_for_thread(chat_id, message_id)
        )
        if session_id and service.get_session(binding.project_path, session_id) is None:
            session_id = None  # stale mapping (session deleted in UI)
        if session_id is None:
            title_seed = (ask or replied_text or "message").strip().replace("\n", " ")
            title = "Telegram: " + (title_seed[:44] + ("..." if len(title_seed) > 44 else ""))
            session = service.create_session(
                binding.project_path, binding.project_id,
                set_current=False, title=title,
            )
            session_id = session.id
        self._remember_thread(chat_id, message_id, session_id)
        if reply_to_id:
            self._remember_thread(chat_id, reply_to_id, session_id)

        async with httpx.AsyncClient(timeout=60) as client:
            # Instant feedback: 👀 reaction + a "checking…" reply that is later
            # edited into the actual answer.
            await self._react(client, token, chat_id, message_id)
            ack_ids = await self._send_reply(client, token, chat_id, message_id, ACK_TEXT)
            ack_id = ack_ids[0] if ack_ids else None
            if ack_id is not None:
                # Replies to the ack continue this session even mid-turn.
                self._remember_thread(chat_id, ack_id, session_id)
                await self._save_state_async()

            typing = asyncio.create_task(self._typing_loop(client, token, chat_id))
            try:
                # Pull the full log from Graylog when the alert references one.
                graylog_block = await graylog_service.resolve_logs_from_text(
                    binding.graylog, replied_text, text
                )
                prompt = self._build_prompt(message, ask, replied_text, graylog_block)

                # Serialize turns per session (two mentions on the same thread
                # must not interleave writes to the session file).
                lock = self._session_locks.setdefault(session_id, asyncio.Lock())
                async with lock:
                    response = await service.send_message(
                        binding.project_path,
                        binding.project_id,
                        prompt,
                        session_id=session_id,
                        register_running=False,
                    )

                reply_text = (response or "").strip() or (
                    "⚠️ The agent returned an empty response. "
                    "Check the chat session in the MagesticAI UI."
                )
                session_link = self._session_url(binding.project_id, session_id)
                if session_link:
                    reply_text += f"\n\n💬 Чат в MagesticAI: {session_link}"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Telegram] Turn failed for chat {chat_id}", exc_info=True)
                reply_text = f"⚠️ Failed to process the request: {e}"
            finally:
                typing.cancel()

            sent_ids = await self._deliver_answer(
                client, token, chat_id, message_id, ack_id, reply_text
            )
            for sid in sent_ids:
                self._remember_thread(chat_id, sid, session_id)
            await self._save_state_async()

    @staticmethod
    def _session_url(project_id: str, session_id: str) -> str:
        """Deep link to the chat session in the web UI (empty when no PUBLIC_URL)."""
        from ..config import get_settings

        base = get_settings().PUBLIC_URL.strip().rstrip("/")
        if not base:
            return ""
        return f"{base}/p/{project_id}/insights?session={session_id}"


_service: TelegramBotService | None = None


def get_telegram_service() -> TelegramBotService:
    global _service
    if _service is None:
        _service = TelegramBotService()
    return _service
