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

Seeding (and re-seeding from the host):
    Option 1 — mount ``/home/saya/.claude/.credentials.json`` into the
    container at ``/home/magesticai/.claude-seed.json``. The service copies
    it to ``~/.claude/.credentials.json`` when the container has no
    credentials, and also re-adopts it on (re)start when the container's
    own credentials have gone stale and the host seed is fresher. Anthropic
    rotates the refresh token on every refresh, so a single subscription
    can't be shared by two clients that both refresh — the container loses
    the race whenever the host refreshes. Re-seeding lets a host re-login
    propagate into the container on the next restart/redeploy. (The seed is
    a single-file bind mount, so the container must be re-created — a deploy
    force-recreate — to see a host file the host replaced via atomic rename.)
    Option 2 — set ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN`` env var alongside
    ``CLAUDE_CODE_OAUTH_TOKEN``. The service constructs a minimal
    credentials.json from these (only when there is no usable seed).
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
        """Populate or refresh ~/.claude/.credentials.json from the host seed or env.

        Re-seeds from the host's mounted credentials not only when the
        container has none, but also when the ones it holds are stale (past
        the refresh buffer) and the host seed is genuinely fresher — so a host
        re-login propagates into the container on the next (re)start.
        """
        _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        current = self._read_creds()

        # A still-valid container token wins: don't clobber it with a possibly
        # older host copy. We only (re)seed when we have nothing, or the token
        # is expired/near-expiry (meaning a refresh has likely been failing).
        if current and not self._needs_refresh(current):
            return

        # Option 1: adopt the host seed when it is fresher than what we hold.
        seed = self._read_seed()
        if seed is not None:
            current_expiry = int(
                (current or {}).get("claudeAiOauth", {}).get("expiresAt") or 0
            )
            seed_expiry = int(seed.get("claudeAiOauth", {}).get("expiresAt") or 0)
            if seed_expiry > current_expiry:
                try:
                    _CREDS_PATH.write_text(json.dumps(seed, indent=2))
                    try:
                        _CREDS_PATH.chmod(0o600)
                    except OSError:
                        pass
                    logger.info(
                        f"[ClaudeToken] (re)seeded credentials from host {_SEED_PATH}"
                    )
                    return
                except OSError as exc:
                    logger.warning(f"[ClaudeToken] seed write failed: {exc}")

        # Keep stale-but-present creds (the CLI refresh path may still revive
        # them); only bootstrap from env vars when we have no credentials and
        # no usable seed.
        if current is not None:
            return

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

    def _read_seed(self) -> dict | None:
        """Read the host-mounted seed credentials, if present and well-formed."""
        if not _SEED_PATH.exists():
            return None
        try:
            data = json.loads(_SEED_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[ClaudeToken] seed read failed: {exc}")
            return None
        if not data.get("claudeAiOauth", {}).get("accessToken"):
            return None
        return data

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

        # The CLI prefers CLAUDE_CODE_OAUTH_TOKEN env var over the credentials
        # file. If that env var holds an expired access token, the CLI hits
        # 401 and never falls through to the refresh path. Strip both OAuth
        # env vars so the CLI uses ~/.claude/.credentials.json (which we keep
        # current) and triggers its native refresh logic on expiry.
        sub_env = {
            k: v
            for k, v in os.environ.items()
            if k not in {
                "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
                "ANTHROPIC_API_KEY",
            }
        }
        sub_env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        sub_env["CI"] = "true"

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
                env=sub_env,
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
        # token was rejected). Capture BOTH streams: the `claude` CLI
        # writes auth errors like "Failed to authenticate. API Error: 401"
        # to stdout, not stderr, so stderr-only logging would just show
        # exit=1 with nothing useful.
        stderr_text = (stderr or b"").decode("utf-8", "replace")[:500].strip()
        stdout_text = (stdout or b"").decode("utf-8", "replace")[:500].strip()
        logger.warning(
            f"[ClaudeToken] CLI refresh did not update credentials "
            f"(exit={proc.returncode}). stderr: {stderr_text!r} | "
            f"stdout: {stdout_text!r}"
        )
        return None

    async def _periodic_refresh(self) -> None:
        """Background loop that nudges the token before it gets close to expiry."""
        try:
            while True:
                await asyncio.sleep(PERIODIC_REFRESH_INTERVAL_SECONDS)
                try:
                    # Re-adopt a fresher host seed if our own creds have gone
                    # bad (e.g. refresh token lost / rotated out by the host),
                    # so the container self-heals without a manual restart.
                    self._seed_if_needed()
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
