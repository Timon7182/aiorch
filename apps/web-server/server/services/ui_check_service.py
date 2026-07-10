"""UI-check parameter resolution: target URL, named environments, credentials.

Used by agent_service when starting a ``taskType == "ui_check"`` task, and by
the insights chat provider when the UI-check toggle is on.

Named environments live in the project's ``deploy.config.json``:

    {
      "environments": {
        "test": {"url": "http://192.168.88.55:3100", "credsPrefix": "UI_CHECK_TEST"}
      }
    }

Credentials live in ``<project>/.magestic-ai/.env`` (never in config, metadata
or prompts): ``<prefix>_USERNAME`` / ``<prefix>_PASSWORD``, with an optional
role-specific override ``<prefix>_<ROLE>_USERNAME`` / ``..._PASSWORD``. The
generic fallback prefix is ``UI_CHECK``. Resolved values are exported to the
agent subprocess env under the canonical names ``UI_CHECK_USERNAME`` /
``UI_CHECK_PASSWORD`` — the model only ever sees the ``${UI_CHECK_USERNAME}``
placeholder; the MCP secret proxy substitutes/redacts the real values.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Canonical env var names the agent's placeholders refer to.
CANONICAL_USERNAME_VAR = "UI_CHECK_USERNAME"
CANONICAL_PASSWORD_VAR = "UI_CHECK_PASSWORD"
DEFAULT_CREDS_PREFIX = "UI_CHECK"


def is_valid_target_url(url: str | None) -> bool:
    """Only http(s) URLs with a host are acceptable browser targets."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def load_environments(project_path: Path) -> dict:
    """Return the ``environments`` map from deploy.config.json ({} if none)."""
    try:
        from .deploy_config import load_deploy_config

        envs = load_deploy_config(project_path).get("environments") or {}
        return envs if isinstance(envs, dict) else {}
    except (ValueError, OSError) as e:
        logger.warning("[UiCheck] Could not load deploy config: %s", e)
        return {}


def _role_token(role: str) -> str:
    """'QA lead' -> 'QA_LEAD' for use in env var names."""
    return re.sub(r"[^A-Za-z0-9]+", "_", role.strip()).strip("_").upper()


def resolve_ui_check_target(
    project_path: Path,
    ui_check_meta: dict | None,
    preview_url: str | None = None,
) -> tuple[str | None, dict]:
    """Resolve the target URL for a UI check.

    Priority: explicit ``uiCheck.url`` → named environment from
    deploy.config.json → running preview URL. Returns ``(url, environment_entry)``
    where ``environment_entry`` is the matched named-environment dict ({} when
    none). A ``None`` url means the agent must BLOCK and ask.
    """
    meta = ui_check_meta or {}
    env_entry: dict = {}

    env_name = (meta.get("environment") or "").strip()
    if env_name:
        entry = load_environments(project_path).get(env_name)
        if isinstance(entry, dict):
            env_entry = entry
        else:
            logger.warning(
                "[UiCheck] Named environment %r not found in deploy.config.json",
                env_name,
            )

    for candidate in (meta.get("url"), env_entry.get("url"), preview_url):
        if is_valid_target_url(candidate):
            return str(candidate).strip(), env_entry
        if candidate:
            logger.warning("[UiCheck] Rejected non-http(s) target URL: %r", candidate)

    return None, env_entry


def resolve_ui_check_credentials(
    available_env: dict,
    ui_check_meta: dict | None,
    environment_entry: dict | None = None,
) -> dict[str, str]:
    """Pick the credential pair for this check from available env vars.

    ``available_env`` is the (already merged) env mapping that includes the
    project's ``.magestic-ai/.env`` values. Tries, in order:

    1. ``<credsPrefix>_<ROLE>_*``  (role-specific, named environment)
    2. ``<credsPrefix>_*``          (named environment)
    3. ``UI_CHECK_<ROLE>_*``        (role-specific, generic)
    4. ``UI_CHECK_*``               (generic)

    A pair counts only if the PASSWORD var is present. Returns the env vars to
    set on the agent subprocess: the canonical UI_CHECK_USERNAME/PASSWORD pair
    plus ``UI_CHECK_SECRET_VARS`` naming them — or {} when no credentials are
    configured (the check then runs unauthenticated / BLOCKs on a login wall).
    """
    meta = ui_check_meta or {}
    entry = environment_entry or {}

    prefixes: list[str] = []
    creds_prefix = str(entry.get("credsPrefix") or "").strip().strip("_")
    role = str(meta.get("role") or "").strip()
    role_token = _role_token(role) if role else ""

    if creds_prefix and role_token:
        prefixes.append(f"{creds_prefix}_{role_token}")
    if creds_prefix:
        prefixes.append(creds_prefix)
    if role_token:
        prefixes.append(f"{DEFAULT_CREDS_PREFIX}_{role_token}")
    prefixes.append(DEFAULT_CREDS_PREFIX)

    for prefix in prefixes:
        password = available_env.get(f"{prefix}_PASSWORD")
        if not password:
            continue
        username = available_env.get(f"{prefix}_USERNAME") or ""
        result = {
            CANONICAL_PASSWORD_VAR: password,
            "UI_CHECK_SECRET_VARS": CANONICAL_PASSWORD_VAR,
        }
        if username:
            result[CANONICAL_USERNAME_VAR] = username
            result["UI_CHECK_SECRET_VARS"] = (
                f"{CANONICAL_USERNAME_VAR},{CANONICAL_PASSWORD_VAR}"
            )
        return result

    return {}
