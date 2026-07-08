"""
Insights AI Chat Service.

Provides AI-powered chat for codebase exploration using multiple LLM providers.
Streams responses via WebSocket and persists sessions to disk.
"""

import asyncio
import base64
import html
import io
import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..websockets.events import broadcast_event
from . import chat_memory
from .branch_worktree import ensure_branch_worktree
from .insights_providers import get_provider

logger = logging.getLogger(__name__)


def _extract_docx_text(raw: bytes) -> str | None:
    """Best-effort text extraction from a .docx, using only the stdlib.

    A ``.docx`` is a zip archive whose body lives in ``word/document.xml``. We
    map paragraph / line-break / tab tags to their whitespace equivalents, strip
    the remaining XML tags, and unescape entities. This is good enough to feed a
    work order, letter, or spec into the prompt — it is not a full converter
    (tables collapse to plain lines, images/embeds are dropped). Returns ``None``
    if the file isn't a readable Word document.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.warning(f"[InsightsService] Could not read .docx body: {e}")
        return None
    # Paragraph and break boundaries -> newlines; tabs -> tabs.
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:br\s*/?>", "\n", xml)
    xml = re.sub(r"<w:tab\s*/?>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _parse_task_json(raw: str) -> dict:
    """Parse a JSON task object from LLM output.

    Tries, in order:
      1. Strip markdown fences and json.loads the whole string
      2. Brace-matching extraction
      3. Fallback: use the raw text as the description
    """
    # Strip markdown fences (```json ... ``` or ``` ... ```)
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

    # Attempt 1: direct parse
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return {
                "title": str(parsed.get("title", "")).strip(),
                "description": str(parsed.get("description", "")).strip(),
            }
    except json.JSONDecodeError:
        pass

    # Attempt 2: brace-matching — find first { … }
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(cleaned[start:i + 1])
                        if isinstance(parsed, dict):
                            return {
                                "title": str(parsed.get("title", "")).strip(),
                                "description": str(parsed.get("description", "")).strip(),
                            }
                    except json.JSONDecodeError:
                        break

    # Attempt 3: use raw text as description
    return {"title": "", "description": cleaned.strip()}


@dataclass
class InsightsMessage:
    """A single chat message."""
    id: str
    role: str  # 'user' or 'assistant'
    content: str
    timestamp: str
    suggested_task: dict | None = None
    tools_used: list | None = None
    provider: str | None = None        # e.g. 'claude', 'ollama', 'codex'
    provider_model: str | None = None   # e.g. 'opus', 'llama3:8b'
    # Attachments sent with this message, stripped of base64 `data` (only
    # metadata + image thumbnail kept, for history display). User messages only.
    attachments: list | None = None


@dataclass
class InsightsSession:
    """A chat session with history."""
    id: str
    project_id: str
    title: str
    messages: list[InsightsMessage] = field(default_factory=list)
    model_config: dict | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # The Claude CLI's own conversation id for this chat. Persisted so each turn
    # can `--resume` the prior one: the CLI keeps the full transcript on disk and
    # we only pass the new message, instead of re-feeding the whole history.
    # Re-captured every turn (the CLI may fork to a new id on resume).
    claude_session_id: str | None = None


class InsightsService:
    """Service for AI-powered insights chat."""

    def __init__(self):
        self._running_tasks: dict[str, asyncio.Task] = {}  # projectId -> running asyncio task
        self._sessions: dict[str, InsightsSession] = {}  # Cache
        # Strong refs to fire-and-forget memory-store tasks so they aren't GC'd
        # mid-flight (asyncio only holds weak references to bare tasks).
        self._memory_tasks: set[asyncio.Task] = set()

    def _get_sessions_dir(self, project_path: Path) -> Path:
        """Get the directory for storing insight sessions."""
        sessions_dir = project_path / ".magestic-ai" / "insights"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir

    def _get_session_file(self, project_path: Path, session_id: str) -> Path:
        """Get the file path for a specific session."""
        return self._get_sessions_dir(project_path) / f"{session_id}.json"

    def _get_current_session_file(self, project_path: Path) -> Path:
        """Get the file that tracks the current active session."""
        return self._get_sessions_dir(project_path) / "current_session.txt"

    def _save_session(self, project_path: Path, session: InsightsSession) -> None:
        """Save a session to disk."""
        session.updated_at = datetime.now().isoformat()
        session_file = self._get_session_file(project_path, session.id)

        data = {
            "id": session.id,
            "projectId": session.project_id,
            "title": session.title,
            "messages": [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp,
                    "suggestedTask": msg.suggested_task,
                    "toolsUsed": msg.tools_used,
                    "provider": msg.provider,
                    "providerModel": msg.provider_model,
                    "attachments": msg.attachments,
                }
                for msg in session.messages
            ],
            "modelConfig": session.model_config,
            "claudeSessionId": session.claude_session_id,
            "createdAt": session.created_at,
            "updatedAt": session.updated_at,
        }

        with open(session_file, 'w') as f:
            json.dump(data, f, indent=2)

        self._sessions[session.id] = session

    def _load_session(self, project_path: Path, session_id: str) -> InsightsSession | None:
        """Load a session from disk."""
        if session_id in self._sessions:
            return self._sessions[session_id]

        session_file = self._get_session_file(project_path, session_id)
        if not session_file.exists():
            return None

        try:
            with open(session_file) as f:
                data = json.load(f)

            session = InsightsSession(
                id=data["id"],
                project_id=data.get("projectId", ""),
                title=data.get("title", "New Session"),
                messages=[
                    InsightsMessage(
                        id=msg["id"],
                        role=msg["role"],
                        content=msg["content"],
                        timestamp=msg["timestamp"],
                        suggested_task=msg.get("suggestedTask"),
                        tools_used=msg.get("toolsUsed"),
                        provider=msg.get("provider"),
                        provider_model=msg.get("providerModel"),
                        attachments=msg.get("attachments"),
                    )
                    for msg in data.get("messages", [])
                ],
                model_config=data.get("modelConfig"),
                claude_session_id=data.get("claudeSessionId"),
                created_at=data.get("createdAt", datetime.now().isoformat()),
                updated_at=data.get("updatedAt", datetime.now().isoformat()),
            )

            self._sessions[session_id] = session
            return session
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load session {session_id}: {e}")
            return None

    def get_current_session(self, project_path: Path, project_id: str) -> InsightsSession:
        """Get or create the current session for a project."""
        current_file = self._get_current_session_file(project_path)

        if current_file.exists():
            session_id = current_file.read_text().strip()
            session = self._load_session(project_path, session_id)
            if session:
                return session

        return self.create_session(project_path, project_id)

    # Default model config for new sessions
    DEFAULT_MODEL_CONFIG = {
        "provider": "claude",
        "model": "sonnet",
        "thinkingLevel": "medium",
    }

    # Cap for inlining a text attachment's contents into the prompt body (used
    # for non-Claude providers, which receive the message via an API body rather
    # than argv). Claude instead reads the file from disk, so it isn't capped here.
    MAX_INLINE_TEXT_CHARS = 100_000

    def create_session(self, project_path: Path, project_id: str) -> InsightsSession:
        """Create a new session."""
        session = InsightsSession(
            id=str(uuid.uuid4()),
            project_id=project_id,
            title="New Session",
            model_config=dict(self.DEFAULT_MODEL_CONFIG),
        )

        self._save_session(project_path, session)

        current_file = self._get_current_session_file(project_path)
        current_file.write_text(session.id)

        return session

    def switch_session(self, project_path: Path, session_id: str) -> InsightsSession | None:
        """Switch to a different session."""
        session = self._load_session(project_path, session_id)
        if session:
            current_file = self._get_current_session_file(project_path)
            current_file.write_text(session_id)
        return session

    def list_sessions(self, project_path: Path) -> list[dict]:
        """List all sessions for a project."""
        try:
            sessions_dir = self._get_sessions_dir(project_path)
        except OSError as e:
            # e.g. PermissionError creating .magestic-ai in a dir the server user
            # can't write. Degrade to "no sessions" instead of a 500 that blanks
            # the Insights view in the frontend.
            logger.warning(f"[InsightsService] Cannot access sessions dir for {project_path}: {e}")
            return []
        sessions = []

        for session_file in sessions_dir.glob("*.json"):
            if session_file.name == "current_session.txt":
                continue
            try:
                with open(session_file) as f:
                    data = json.load(f)
                sessions.append({
                    "id": data["id"],
                    "title": data.get("title", "Untitled"),
                    "messageCount": len(data.get("messages", [])),
                    "createdAt": data.get("createdAt"),
                    "updatedAt": data.get("updatedAt"),
                })
            except (json.JSONDecodeError, KeyError):
                continue

        sessions.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
        return sessions

    def delete_session(self, project_path: Path, session_id: str) -> dict:
        """Delete a session. Returns info about what happened."""
        session_file = self._get_session_file(project_path, session_id)
        if not session_file.exists():
            return {"deleted": False}

        session_file.unlink()

        # Remove any attachment files written for this session's messages.
        attachments_dir = self._get_sessions_dir(project_path) / "attachments" / session_id
        if attachments_dir.exists():
            shutil.rmtree(attachments_dir, ignore_errors=True)

        if session_id in self._sessions:
            del self._sessions[session_id]

        was_current = False
        current_file = self._get_current_session_file(project_path)
        if current_file.exists() and current_file.read_text().strip() == session_id:
            current_file.unlink()
            was_current = True

        # If the deleted session was the current one, switch to the most recent remaining session
        switched_to = None
        if was_current:
            remaining = self.list_sessions(project_path)
            if remaining:
                # Switch to the most recent session (list is already sorted by updatedAt desc)
                next_session_id = remaining[0]["id"]
                current_file.write_text(next_session_id)
                switched_to = next_session_id

        return {"deleted": True, "switchedTo": switched_to}

    def rename_session(self, project_path: Path, session_id: str, new_title: str) -> bool:
        """Rename a session."""
        session = self._load_session(project_path, session_id)
        if session:
            session.title = new_title
            self._save_session(project_path, session)
            return True
        return False

    def update_model_config(self, project_path: Path, session_id: str, model_config: dict) -> bool:
        """Update model config for a session."""
        session = self._load_session(project_path, session_id)
        if session:
            session.model_config = model_config
            self._save_session(project_path, session)
            return True
        return False

    def _prepare_attachments(
        self,
        project_path: Path,
        session_id: str,
        turn_id: str,
        attachments: list | None,
    ) -> tuple[str, str, Path | None]:
        """Write attachments to disk and build per-provider prompt suffixes.

        Returns ``(claude_suffix, generic_suffix, attachment_dir)``:

        - ``claude_suffix`` — instructs the agent to ``Read`` the written files
          (text, images, PDFs, and extracted-DOCX text). Keeping large content
          off the CLI argv avoids command-line length limits (notably 32 KB on
          Windows) and lets Claude view images/PDFs as vision input.
        - ``generic_suffix`` — inlines text-file and extracted-DOCX contents
          (truncated) and notes images/PDFs, for providers without file tools
          (they get the message via an API body, so length isn't an argv concern).
        - ``attachment_dir`` — the per-turn directory to grant the Claude CLI via
          ``--add-dir`` so it can read the files regardless of cwd / branch
          worktree. ``None`` when nothing was written.
        """
        if not attachments:
            return "", "", None

        turn_dir = (
            project_path / ".magestic-ai" / "insights" / "attachments" / session_id / turn_id
        )
        written_any = False
        read_paths: list[Path] = []   # text + image + doc paths for the Claude Read instruction
        inline_blocks: list[str] = [] # inlined text contents for generic providers
        image_notes: list[str] = []   # image filenames noted for generic providers
        doc_notes: list[str] = []     # binary doc (PDF) filenames noted for generic providers

        for att in attachments:
            if not isinstance(att, dict):
                continue
            kind = att.get("kind")
            raw_name = str(att.get("filename") or "attachment")
            # Sanitize: drop any path components so a crafted filename can't
            # escape the turn dir (mirrors save_changelog_image).
            safe_name = raw_name.replace("/", "_").replace("\\", "_").lstrip(".") or "attachment"
            data_b64 = att.get("data") or ""

            if kind == "image":
                try:
                    turn_dir.mkdir(parents=True, exist_ok=True)
                    dest = turn_dir / safe_name
                    dest.write_bytes(base64.b64decode(data_b64))
                    dest.chmod(0o600)
                    written_any = True
                    read_paths.append(dest)
                    image_notes.append(safe_name)
                except Exception as e:
                    logger.warning(f"[InsightsService] Failed to write image attachment {safe_name!r}: {e}")

            elif kind == "text":
                try:
                    content = base64.b64decode(data_b64).decode("utf-8", errors="replace")
                except Exception as e:
                    logger.warning(f"[InsightsService] Failed to decode text attachment {safe_name!r}: {e}")
                    continue
                # Claude path: write to disk so it can Read (no argv bloat).
                try:
                    turn_dir.mkdir(parents=True, exist_ok=True)
                    dest = turn_dir / safe_name
                    dest.write_text(content, encoding="utf-8")
                    dest.chmod(0o600)
                    written_any = True
                    read_paths.append(dest)
                except Exception as e:
                    logger.warning(f"[InsightsService] Failed to write text attachment {safe_name!r}: {e}")
                # Generic path: inline the (truncated) contents.
                snippet = content
                if len(snippet) > self.MAX_INLINE_TEXT_CHARS:
                    snippet = snippet[: self.MAX_INLINE_TEXT_CHARS] + "\n… [truncated]"
                inline_blocks.append(f"\n\n--- Attached file: {safe_name} ---\n```\n{snippet}\n```")

            elif kind == "document":
                try:
                    raw = base64.b64decode(data_b64)
                except Exception as e:
                    logger.warning(f"[InsightsService] Failed to decode document attachment {safe_name!r}: {e}")
                    continue
                lower = safe_name.lower()
                is_pdf = lower.endswith(".pdf") or att.get("mimeType") == "application/pdf"

                if is_pdf:
                    # Claude's Read tool renders PDFs natively (text + layout), so
                    # write the raw bytes and let the agent read them. Generic
                    # providers can't ingest a binary PDF, so we only note it.
                    try:
                        turn_dir.mkdir(parents=True, exist_ok=True)
                        dest = turn_dir / safe_name
                        dest.write_bytes(raw)
                        dest.chmod(0o600)
                        written_any = True
                        read_paths.append(dest)
                        doc_notes.append(safe_name)
                    except Exception as e:
                        logger.warning(f"[InsightsService] Failed to write PDF attachment {safe_name!r}: {e}")
                    continue

                # .docx (or other Word doc): the Read tool can't parse the binary
                # zip, so extract text server-side and write it as a .txt sidecar.
                text = _extract_docx_text(raw)
                if not text:
                    doc_notes.append(safe_name)
                    logger.warning(f"[InsightsService] No extractable text in document {safe_name!r}")
                    continue
                try:
                    turn_dir.mkdir(parents=True, exist_ok=True)
                    dest = turn_dir / f"{safe_name}.txt"
                    dest.write_text(text, encoding="utf-8")
                    dest.chmod(0o600)
                    written_any = True
                    read_paths.append(dest)
                except Exception as e:
                    logger.warning(f"[InsightsService] Failed to write document text for {safe_name!r}: {e}")
                # Generic path: inline the (truncated) extracted text.
                snippet = text
                if len(snippet) > self.MAX_INLINE_TEXT_CHARS:
                    snippet = snippet[: self.MAX_INLINE_TEXT_CHARS] + "\n… [truncated]"
                inline_blocks.append(
                    f"\n\n--- Attached document (extracted text): {safe_name} ---\n```\n{snippet}\n```"
                )

        claude_suffix = ""
        if read_paths:
            listed = ", ".join(str(p) for p in read_paths)
            claude_suffix = (
                "\n\n[The user attached file(s) for this message. "
                f"Use the Read tool to view them: {listed}]"
            )

        generic_parts = list(inline_blocks)
        if image_notes:
            generic_parts.append(
                f"\n\n[The user also attached image(s): {', '.join(image_notes)}. "
                "Image viewing is only supported with the Claude provider.]"
            )
        if doc_notes:
            generic_parts.append(
                f"\n\n[The user also attached document(s): {', '.join(doc_notes)}. "
                "Reading PDF documents is only supported with the Claude provider.]"
            )
        generic_suffix = "".join(generic_parts)

        return claude_suffix, generic_suffix, (turn_dir if written_any else None)

    async def send_message(
        self,
        project_path: Path,
        project_id: str,
        message: str,
        model_config: dict | None = None,
        branch: str | None = None,
        repo_path: Path | None = None,
        attachments: list | None = None,
    ) -> None:
        """Send a message and stream the response via the appropriate provider.

        When ``branch`` names a branch other than the current checkout, the
        provider runs against a read-only worktree of that branch so the chat
        can answer using its contents without disturbing the user's working
        tree. Sessions and usage still belong to the main project directory.

        ``repo_path`` scopes the *grounding* to a child repo of a multi-repo
        project (e.g. ``cts/backend`` under the ``cts`` parent). When set, the
        provider runs in that repo and the branch worktree is built from it,
        while sessions/usage stay keyed to ``project_path`` (the parent) so chat
        history is shared across repos. None => ground in ``project_path``.
        """
        # Where to ground/cwd the provider. Sessions always use project_path.
        ground_dir = repo_path or project_path
        # NOTE: session loading/saving is inside the try below on purpose. This
        # coroutine runs as a fire-and-forget asyncio task (see start_message), so
        # any exception raised here is otherwise silently dropped ("Task exception
        # was never retrieved") and the frontend hangs forever with no error. e.g.
        # a PermissionError creating .magestic-ai/insights must reach the client.
        try:
            # Get current session
            session = self.get_current_session(project_path, project_id)

            # Merge session config with message-level config (message takes precedence)
            effective_config = dict(self.DEFAULT_MODEL_CONFIG)
            if session.model_config:
                effective_config.update({k: v for k, v in session.model_config.items() if v is not None})
            if model_config:
                effective_config.update({k: v for k, v in model_config.items() if v is not None})
            model_config = effective_config

            # Determine provider from model_config (default: claude)
            provider_id = model_config.get("provider", "claude")
            provider_model = model_config.get("model", "sonnet")

            # Add user message. Persist only attachment metadata (+ image
            # thumbnail) — never the full base64 `data`, which is large and only
            # needed transiently to write the files below.
            stored_attachments = None
            if attachments:
                stored_attachments = [
                    {
                        "id": att.get("id"),
                        "kind": att.get("kind"),
                        "filename": att.get("filename"),
                        "mimeType": att.get("mimeType"),
                        "size": att.get("size"),
                        **({"thumbnail": att.get("thumbnail")} if att.get("thumbnail") else {}),
                    }
                    for att in attachments
                    if isinstance(att, dict)
                ]

            user_msg = InsightsMessage(
                id=f"msg-{uuid.uuid4().hex[:8]}",
                role="user",
                content=message,
                timestamp=datetime.now().isoformat(),
                attachments=stored_attachments,
            )
            session.messages.append(user_msg)

            # Auto-generate title from first message
            if session.title == "New Session" and len(session.messages) == 1:
                title_seed = message.strip() or "Attachment"
                session.title = title_seed[:50] + ("..." if len(title_seed) > 50 else "")

            self._save_session(project_path, session)

            # Materialize attachments to disk and build the prompt augmentation.
            # The stored message content stays clean (the original text); only the
            # text sent to the provider carries the inlined files / Read pointers.
            claude_suffix, generic_suffix, attachment_dir = self._prepare_attachments(
                project_path, session.id, user_msg.id, attachments
            )
            suffix = claude_suffix if provider_id == "claude" else generic_suffix
            final_message = message
            if suffix:
                if not final_message.strip():
                    final_message = "Please review the attached file(s) and respond."
                final_message = final_message + suffix

            # Long-term memory recall (Graphiti): prepend any facts relevant to
            # this turn so the model carries context across separate chats. A
            # no-op (returns "") unless Graphiti is enabled for this deployment.
            # Bounded by a timeout so a slow embedding search / first-call DB
            # init never stalls the reply — we'd rather skip recall than make
            # the user wait before the model starts streaming.
            recalled = ""
            if chat_memory.is_enabled():
                try:
                    recalled = await asyncio.wait_for(
                        chat_memory.recall(project_path, message), timeout=2.5
                    )
                except asyncio.TimeoutError:
                    logger.info("[InsightsService] memory recall timed out; skipping")
                except Exception as e:
                    logger.info(f"[InsightsService] memory recall skipped: {e}")
            if recalled:
                final_message = f"{recalled}\n\n---\n\n{final_message}"

            # Build conversation history for stateless providers
            conversation_history = [
                {"role": msg.role, "content": msg.content}
                for msg in session.messages[:-1]  # Exclude current user message
            ]

            # Resolve a branch worktree if the user asked to ground the chat in
            # a branch other than the current checkout. None => use ground_dir.
            working_dir = None
            if branch:
                working_dir = await asyncio.to_thread(
                    ensure_branch_worktree, ground_dir, branch
                )
                if working_dir:
                    logger.info(
                        f"[InsightsService] Chatting against branch {branch!r} "
                        f"in worktree {working_dir}"
                    )
                else:
                    logger.info(
                        f"[InsightsService] Branch {branch!r} unavailable or "
                        f"already current; using {ground_dir}"
                    )
            # No branch worktree, but a child repo was selected: ground the
            # provider in that repo (its cwd) rather than the parent project.
            if working_dir is None and ground_dir != project_path:
                working_dir = ground_dir

            # Route to provider
            provider = get_provider(provider_id)
            logger.info(f"[InsightsService] Routing to provider: {provider_id} (model: {provider_model})")

            # Claude-only extras: attachment access, and native session resume so
            # the CLI carries the history itself (we pass only the new message).
            # `session_capture` is an in/out dict the provider fills with the
            # CLI's session id for this turn, which we persist for the next one.
            claude_extras: dict = {}
            session_capture: dict = {}
            if provider_id == "claude":
                claude_extras["attachment_dir"] = attachment_dir
                claude_extras["resume_session_id"] = session.claude_session_id
                claude_extras["session_capture"] = session_capture

            response_content = await provider.send_message(
                project_path=project_path,
                project_id=project_id,
                message=final_message,
                model=provider_model,
                model_config=model_config,
                conversation_history=conversation_history if provider_id != "claude" else None,
                working_dir=working_dir,
                **claude_extras,
            )

            # Persist the CLI session id so the next turn resumes this thread.
            captured_sid = session_capture.get("session_id")
            if captured_sid and captured_sid != session.claude_session_id:
                session.claude_session_id = captured_sid

            # Persist the assistant response to disk
            if response_content and response_content.strip():
                assistant_msg = InsightsMessage(
                    id=f"msg-{uuid.uuid4().hex[:8]}",
                    role="assistant",
                    content=response_content,
                    timestamp=datetime.now().isoformat(),
                    provider=provider_id,
                    provider_model=provider_model,
                )
                session.messages.append(assistant_msg)
                self._save_session(project_path, session)

                # Persist this exchange to long-term memory (Graphiti) in the
                # background so the next chat can recall it. Fire-and-forget:
                # ingestion runs an LLM extraction and must not block the reply.
                # A no-op unless Graphiti is enabled.
                if chat_memory.is_enabled():
                    task = asyncio.create_task(
                        chat_memory.store(project_path, message, response_content)
                    )
                    self._memory_tasks.add(task)
                    task.add_done_callback(self._memory_tasks.discard)

        except asyncio.CancelledError:
            logger.info(f"[InsightsService] Chat cancelled for project {project_id}")
            # Finalize partial content if any
            await broadcast_event("insights:chunk", {
                "projectId": project_id,
                "type": "done",
            })
        except Exception as e:
            logger.error(f"[InsightsService] Provider error: {e}", exc_info=True)
            await broadcast_event("insights:chunk", {
                "projectId": project_id,
                "type": "error",
                "error": str(e),
            })
        finally:
            self._running_tasks.pop(project_id, None)

    def start_message(
        self,
        project_path: Path,
        project_id: str,
        message: str,
        model_config: dict | None = None,
        branch: str | None = None,
        repo_path: Path | None = None,
        attachments: list | None = None,
    ) -> None:
        """Start send_message as a tracked background task."""
        # Cancel any existing running task for this project
        self.stop_message(project_id)

        task = asyncio.create_task(
            self.send_message(
                project_path, project_id, message, model_config, branch, repo_path, attachments
            )
        )
        self._running_tasks[project_id] = task

    def stop_message(self, project_id: str) -> bool:
        """Cancel the running chat task for a project. Returns True if a task was cancelled."""
        task = self._running_tasks.pop(project_id, None)
        if task and not task.done():
            task.cancel()
            logger.info(f"[InsightsService] Cancelled running task for project {project_id}")
            return True
        return False

    async def generate_task_from_chat(
        self,
        project_path: Path,
        project_id: str,
        model_config: dict | None = None,
    ) -> dict:
        """Summarize the current chat session into a structured task.

        Runs a lightweight ``claude --print`` call (no tool use,
        no streaming) to produce a JSON ``{title, description}`` object.
        """
        import os
        import shutil

        session = self.get_current_session(project_path, project_id)
        if not session or not session.messages:
            return {"title": "", "description": ""}

        # Build transcript
        transcript_lines: list[str] = []
        for msg in session.messages:
            role = "User" if msg.role == "user" else "Assistant"
            transcript_lines.append(f"[{role}]: {msg.content}")
        transcript = "\n\n".join(transcript_lines)

        summarization_prompt = (
            "You are a product manager assistant. Based on the following conversation, "
            "create a structured task for a software development backlog.\n\n"
            "Return ONLY a JSON object with exactly two keys:\n"
            '- "title": a concise task title (max 80 chars)\n'
            '- "description": a PRD-style description with context, requirements, '
            "and acceptance criteria in markdown\n\n"
            "Conversation transcript:\n"
            f"{transcript}\n\n"
            "Respond with ONLY the JSON object, no other text."
        )

        # Resolve model
        effective_config = dict(self.DEFAULT_MODEL_CONFIG)
        if session.model_config:
            effective_config.update({k: v for k, v in session.model_config.items() if v is not None})
        if model_config:
            effective_config.update({k: v for k, v in model_config.items() if v is not None})

        # Use session's configured model, defaulting to haiku for fast summarization
        model_value = effective_config.get("model", "haiku")

        # Resolve Claude CLI path
        claude_bin = shutil.which("claude") or "claude"

        # Lightweight call: --print (non-interactive, single response)
        cmd = [claude_bin, "--print", "--model", model_value, summarization_prompt]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("CLAUDECODE", None)

        # Resolve OAuth token (reuse Claude provider logic)
        try:
            provider = get_provider("claude")
            token, _pid, profile_name = provider._resolve_claude_token()
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token
                logger.info(f"[InsightsService] generate_task using profile: {profile_name}")
        except Exception:
            pass

        logger.info(f"[InsightsService] Generating task via claude --print (model={model_value})")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_path),
                env=env,
            )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            response = stdout.decode("utf-8", errors="replace").strip()

            stderr_text = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            logger.info(
                f"[InsightsService] generate_task CLI finished: "
                f"rc={proc.returncode}, stdout_len={len(response)}, "
                f"stderr_len={len(stderr_text)}"
            )
            if stderr_text:
                logger.info(f"[InsightsService] generate_task stderr: {stderr_text[:500]}")
            if response:
                logger.info(f"[InsightsService] generate_task stdout: {response[:300]}")

            if proc.returncode != 0 and not response:
                logger.error(f"[InsightsService] claude CLI exited {proc.returncode}")
                return {"title": "", "description": ""}

            if response:
                return _parse_task_json(response)
            return {"title": "", "description": ""}

        except asyncio.TimeoutError:
            logger.error("[InsightsService] generate_task_from_chat timed out (120s)")
            return {"title": "", "description": ""}
        except Exception as e:
            logger.error(f"[InsightsService] generate_task_from_chat failed: {e}", exc_info=True)
            return {"title": "", "description": ""}

    def clear_session(self, project_path: Path, project_id: str) -> InsightsSession:
        """Clear the current session and create a new one."""
        current_file = self._get_current_session_file(project_path)

        if current_file.exists():
            session_id = current_file.read_text().strip()
            self.delete_session(project_path, session_id)

        return self.create_session(project_path, project_id)


# Global service instance
_insights_service: InsightsService | None = None


def get_insights_service() -> InsightsService:
    """Get the global insights service instance."""
    global _insights_service
    if _insights_service is None:
        _insights_service = InsightsService()
    return _insights_service
