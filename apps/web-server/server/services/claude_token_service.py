"""Claude OAuth access-token refresh loop.

Anthropic's Max subscription OAuth issues short-lived access tokens (about
1 hour) plus a long-lived refresh token. The Claude Code CLI refreshes
silently as it runs; agents that only see the env var
``CLAUDE_CODE_OAUTH_TOKEN`` from the host's secrets file hit 401 once the
initial token expires.

This service keeps the container's ``~/.claude/.credentials.json`` (the
canonical credential location the CLI itself uses) up to date, and exposes
``get_access_token()`` returning a token guaranteed fresh for the next
~5 minutes. Refreshing is delegated to the CLI: we run a 1-token ``claude
--print`` call that triggers the CLI's own refresh handler. This avoids
hardcoding Anthropic's OAuth token endpoint, client_id, or the exact
refresh-grant body — when Anthropic changes any of that, the CLI
incorporates the change and our code keeps working.

Seeding (first start):
    Option 1 — mount ``/home/saya/.claude/.credentials.json`` into the
    container at ``/home/magesticai/.claude-seed.json``. The service
    copies it to ``~/.claude/.credentials.json`` on first start.
    Option 2 — set ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN`` env var alongside
    ``CLAUDE_CODE_OAUTH_TOKEN``. The service constructs a minimal
    credentials.json from these.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Refresh proactively when the token has less than this many seconds left.
# The CLI considers a token expired-ish at ~5 min, so we mirror that.
REFRESH_BUFFER_SECONDS = 5 * 60

# Maximum time to wait for the CLI refresh subprocess.
REFRESH_SUBPROCESS_TIMEOUT_SECONDS = 30

# Periodic refresh interval — we proactively refresh every 30 minutes so the
# first request after a long idle period doesn't pay the refresh latency.
PERIODIC_REFRESH_INTERVAL_SECONDS = 30 * 60

_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
_SEED_PATH = Path.home() / ".claude-seed.json"


class ClaudeTokenService:
    """Owns ~/.claude/.credentials.json + a refresh task."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._last_refresh_attempt: float = 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Seed credentials if missing and start the periodic refresh loop."""
        self._seed_if_needed()
        # Touch creds on startup to validate them; non-fatal if it fails.
        await self.get_access_token()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._periodic_refresh())

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def get_access_token(self) -> str | None:
        """Return a valid access token, refreshing if it expires soon.

        Returns None if no credentials are available (no seed, no env var
        bootstrap). Returns the existing token even when refresh fails so a
        partially-working state stays functional until expiry.
        """
        async with self._lock:
            creds = self._read_creds()
            if not creds:
                return None
            if not self._needs_refresh(creds):
                return _access_token(creds)

            # Avoid hammering refresh if a previous attempt just failed.
            now = time.time()
            if now - self._last_refresh_attempt < 30:
                return _access_token(creds)
            self._last_refresh_attempt = now

            refreshed = await self._refresh_via_cli()
            if refreshed:
                return _access_token(refreshed)
            # Refresh failed — return whatever we have (may still be valid
            # for a couple more minutes, or already expired).
            return _access_token(creds)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _seed_if_needed(self) -> None:
        """Populate ~/.claude/.credentials.json from the seed file or env."""
        if _CREDS_PATH.exists():
            return
        _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Option 1: copy from mounted seed file.
        if _SEED_PATH.exists():
            try:
                data = json.loads(_SEED_PATH.read_text())
                # Validate it's the expected shape.
                if data.get("claudeAiOauth", {}).get("accessToken"):
                    _CREDS_PATH.write_text(json.dumps(data, indent=2))
                    try:
                        _CREDS_PATH.chmod(0o600)
                    except OSError:
                        pass
                    logger.info(
                        f"[ClaudeToken] seeded credentials from {_SEED_PATH}"
                    )
                    return
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"[ClaudeToken] seed read failed: {exc}")

        # Option 2: env var bootstrap.
        access = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        refresh = os.environ.get("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "").strip()
        if access and refresh:
            data = {
                "claudeAiOauth": {
                    "accessToken": access,
                    "refreshToken": refresh,
                    # Mark as expired immediately so the first call refreshes.
                    "expiresAt": int(time.time() * 1000),
                    "scopes": ["user:inference", "user:profile"],
                },
            }
            try:
                _CREDS_PATH.write_text(json.dumps(data, indent=2))
                _CREDS_PATH.chmod(0o600)
                logger.info(
                    "[ClaudeToken] seeded credentials from CLAUDE_CODE_OAUTH_TOKEN "
                    "+ CLAUDE_CODE_OAUTH_REFRESH_TOKEN env vars"
                )
            except OSError as exc:
                logger.warning(f"[ClaudeToken] env-seed write failed: {exc}")
        elif access and not refresh:
            logger.warning(
                "[ClaudeToken] CLAUDE_CODE_OAUTH_TOKEN set but no refresh token "
                "available — tokens will expire and cannot be auto-refreshed. "
                "Mount /home/saya/.claude/.credentials.json into the container, "
                "or set CLAUDE_CODE_OAUTH_REFRESH_TOKEN."
            )

    def _read_creds(self) -> dict | None:
        if not _CREDS_PATH.exists():
            return None
        try:
            return json.loads(_CREDS_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[ClaudeToken] credentials read failed: {exc}")
            return None

    def _needs_refresh(self, creds: dict) -> bool:
        expires_at_ms = int(creds.get("claudeAiOauth", {}).get("expiresAt") or 0)
        now_ms = int(time.time() * 1000)
        return expires_at_ms - now_ms < REFRESH_BUFFER_SECONDS * 1000

    async def _refresh_via_cli(self) -> dict | None:
        """Run a tiny `claude` invocation; the CLI refreshes if needed.

        Returns the updated credentials dict on success, None on failure.
        """
        creds_before = self._read_creds()
        if not creds_before:
            return None
        refresh_token = creds_before.get("claudeAiOauth", {}).get("refreshToken")
        if not refresh_token:
            logger.warning(
                "[ClaudeToken] cannot refresh — no refresh_token in credentials"
            )
            return None

        # Find the claude binary; container installs it in the magesticai
        # user's npm-global. PATH usually has it but fall back if not.
        claude_bin = shutil.which("claude")
        if claude_bin is None:
            for candidate in (
                "/home/magesticai/.npm-global/bin/claude",
                "/usr/local/bin/claude",
            ):
                if Path(candidate).exists():
                    claude_bin = candidate
                    break
        if claude_bin is None:
            logger.error("[ClaudeToken] `claude` CLI not found on PATH")
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                claude_bin,
                "--print",
                "--model",
                "claude-haiku-4-5-20251001",
                "ok",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli", "CI": "true"},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=REFRESH_SUBPROCESS_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning("[ClaudeToken] CLI refresh subprocess timed out")
                return None
        except Exception as exc:
            logger.warning(f"[ClaudeToken] CLI refresh spawn failed: {exc!r}")
            return None

        creds_after = self._read_creds()
        if not creds_after:
            return None

        new_expires = creds_after.get("claudeAiOauth", {}).get("expiresAt", 0)
        old_expires = creds_before.get("claudeAiOauth", {}).get("expiresAt", 0)
        if new_expires > old_expires:
            logger.info(
                f"[ClaudeToken] refresh OK — new expiry "
                f"+{(new_expires - int(time.time() * 1000)) // 1000}s"
            )
            return creds_after

        # CLI didn't update the file (often because the call itself 401'd
        # because the access token was already expired AND the refresh
        # token was rejected). Surface the stderr so the operator can see
        # what happened.
        stderr_text = (stderr or b"").decode("utf-8", "replace")[:500].strip()
        logger.warning(
            f"[ClaudeToken] CLI refresh did not update credentials "
            f"(exit={proc.returncode}). stderr: {stderr_text}"
        )
        return None

    async def _periodic_refresh(self) -> None:
        """Background loop that nudges the token before it gets close to expiry."""
        try:
            while True:
                await asyncio.sleep(PERIODIC_REFRESH_INTERVAL_SECONDS)
                try:
                    await self.get_access_token()
                except Exception:
                    logger.exception("[ClaudeToken] periodic refresh raised")
        except asyncio.CancelledError:
            pass


def _access_token(creds: dict) -> str | None:
    return creds.get("claudeAiOauth", {}).get("accessToken") or None


_singleton: ClaudeTokenService | None = None


def get_claude_token_service() -> ClaudeTokenService:
    global _singleton
    if _singleton is None:
        _singleton = ClaudeTokenService()
    return _singleton
