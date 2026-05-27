"""LSP Session - wraps a language-server subprocess for WebSocket communication.

Each session owns one language-server process. The browser side
(monaco-languageclient via vscode-ws-jsonrpc) speaks **bare** JSON-RPC over the
WebSocket as text frames (no framing header), while the language server's stdio
uses LSP framing: ``Content-Length: N\\r\\n\\r\\n<json-bytes>``. This session
translates between the two directions.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# Generous StreamReader buffer: hover/completion payloads and the header read
# (readuntil) can exceed the default 64 KiB and raise LimitOverrunError.
_STREAM_LIMIT = 8 * 1024 * 1024

# Env vars stripped from the subprocess environment, mirroring PTYSession.start()
# so a language server never inherits Claude OAuth credentials.
_STRIPPED_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "ANTHROPIC_API_KEY",
)


@dataclass
class LSPSession:
    """A single language-server subprocess session."""

    language: str
    command: list[str]
    root: str
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=datetime.now)

    # Internal state
    _proc: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    async def start(self) -> None:
        """Spawn the language-server process rooted at the workspace."""
        if self._proc is not None:
            raise RuntimeError("LSP session already started")

        env = os.environ.copy()
        for key in _STRIPPED_ENV:
            env.pop(key, None)

        # npm installs language servers as `.cmd` shims on Windows, which
        # CreateProcess (create_subprocess_exec) can't launch directly — wrap
        # them via cmd.exe. On Linux/macOS the shim is a shebang script and runs
        # as-is. (Prod is Linux; this keeps the Windows dev box working.)
        command = self.command
        if os.name == "nt" and command and command[0].lower().endswith((".cmd", ".bat")):
            command = ["cmd", "/c", *command]

        self._proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        self._closed = False
        logger.info(
            "[lsp:%s:%s] started %s (root=%s, pid=%s)",
            self.language,
            self.id[:8],
            self.command[0],
            self.root,
            self._proc.pid,
        )

    def is_alive(self) -> bool:
        """True while the subprocess is running."""
        return self._proc is not None and self._proc.returncode is None

    async def write_message(self, text: str) -> None:
        """WS -> server: frame a bare JSON-RPC message and write it to stdin.

        Drops the message (rather than raising into the receive loop) if the
        process has exited or stdin is closing.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        stdin = proc.stdin
        if stdin is None or stdin.is_closing():
            return

        body = text.encode("utf-8")
        # Content-Length is the BYTE length of the UTF-8 body, not char count —
        # multibyte content otherwise desyncs the stream.
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            stdin.write(header)
            stdin.write(body)
            await stdin.drain()  # backpressure point
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[lsp:%s:%s] stdin write failed: %s", self.language, self.id[:8], exc)

    async def pump_stdout_to_ws(self, send: Callable[[str], Awaitable[None]]) -> None:
        """server -> WS: parse Content-Length framing from stdout, forward bodies.

        Returns when the server closes stdout (EOF) or the stream desyncs.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout

        while True:
            try:
                header_block = await stdout.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                break  # EOF — server closed stdout
            except asyncio.LimitOverrunError:
                logger.error("[lsp:%s:%s] header exceeded buffer limit", self.language, self.id[:8])
                break
            except asyncio.CancelledError:
                raise

            content_length = _parse_content_length(header_block)
            if content_length is None:
                logger.error(
                    "[lsp:%s:%s] missing/invalid Content-Length; terminating stream",
                    self.language,
                    self.id[:8],
                )
                break

            try:
                body = await stdout.readexactly(content_length)
            except asyncio.IncompleteReadError:
                break
            except asyncio.CancelledError:
                raise

            try:
                await send(body.decode("utf-8"))
            except asyncio.CancelledError:
                raise
            except Exception:
                break

    async def drain_stderr(self) -> None:
        """Continuously drain stderr so a full pipe buffer can't block the server.

        Mandatory: pyright/tsserver are chatty, and a full stderr pipe deadlocks
        the language server on its next stderr write.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        while True:
            try:
                line = await stderr.readline()
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            if not line:
                break
            logger.debug(
                "[lsp:%s:%s] stderr: %s",
                self.language,
                self.id[:8],
                line.decode("utf-8", "replace").rstrip(),
            )

    async def close(self) -> None:
        """Terminate the subprocess and reap it (no zombies)."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        self._proc = None

        # Closing stdin signals EOF; most servers self-exit on it.
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass

        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()  # reap
                except Exception:
                    pass
            except Exception:
                pass

        logger.info("[lsp:%s:%s] closed", self.language, self.id[:8])

    def to_dict(self) -> dict:
        """Serialize session metadata."""
        return {
            "id": self.id,
            "language": self.language,
            "root": self.root,
            "created_at": self.created_at.isoformat(),
            "is_alive": self.is_alive(),
        }


def _parse_content_length(header_block: bytes) -> int | None:
    """Extract the Content-Length value from an LSP header block (case-insensitive)."""
    for line in header_block.split(b"\r\n"):
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        if name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None
