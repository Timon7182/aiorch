"""
Prompt Resolver
===============

Single chokepoint for resolving agent prompt files, so that prompts can be
overridden per-project without threading ``project_dir`` through every loader.

The web-server materializes a project's prompt overrides to
``<project>/.magestic-ai/prompts/<key>`` before launching an agent subprocess
and sets ``MAGESTIC_PROMPT_OVERRIDE_DIR`` to that directory. This module checks
that directory first and falls back to the bundled prompts shipped in
``apps/backend/prompts/``.

A prompt's canonical identifier (``rel_key``) is its path relative to the
prompts root, e.g. ``"planner.md"``, ``"qa_reviewer.md"``,
``"github/pr_reviewer.md"``. When the override env var is unset (e.g. in unit
tests or direct CLI use without the web-server), resolution returns the bundled
file unchanged, so behavior is identical to before.
"""

import os
from pathlib import Path

# Bundled prompts live in apps/backend/prompts/ (sibling of prompts_pkg/).
BUNDLED_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# The env var the web-server sets to the per-project override directory.
OVERRIDE_DIR_ENV = "MAGESTIC_PROMPT_OVERRIDE_DIR"

# A few prompts do not live under apps/backend/prompts/. To keep a single
# override root, they are exposed under a synthetic key and we map that key back
# to its real bundled location here. The override path still mirrors the key.
_SPECIAL_BUNDLED_PATHS: dict[str, Path] = {
    # insight_extractor.md actually lives in apps/backend/analysis/prompts/
    "analysis/insight_extractor.md": (
        Path(__file__).parent.parent / "analysis" / "prompts" / "insight_extractor.md"
    ),
}


def _override_dir() -> Path | None:
    """Return the configured per-project override directory, if any."""
    raw = os.environ.get(OVERRIDE_DIR_ENV)
    if not raw:
        return None
    return Path(raw)


def bundled_prompt_path(rel_key: str) -> Path:
    """Return the bundled (default) path for a prompt key, ignoring overrides."""
    special = _SPECIAL_BUNDLED_PATHS.get(rel_key)
    if special is not None:
        return special
    return BUNDLED_PROMPTS_DIR / rel_key


def resolve_prompt_file(rel_key: str) -> Path:
    """Resolve a prompt key to an on-disk file.

    Checks ``MAGESTIC_PROMPT_OVERRIDE_DIR/<rel_key>`` first; if that file
    exists, the project has customized this prompt. Otherwise falls back to the
    bundled default.

    Args:
        rel_key: Prompt path relative to the prompts root
            (e.g. ``"planner.md"``, ``"github/pr_reviewer.md"``).

    Returns:
        Path to the file that should be read. The path is not guaranteed to
        exist (callers that previously checked ``.exists()`` should keep doing
        so) — only that the override is preferred when present.
    """
    override_dir = _override_dir()
    if override_dir is not None:
        candidate = override_dir / rel_key
        if candidate.is_file():
            return candidate
    return bundled_prompt_path(rel_key)


def read_prompt(rel_key: str) -> str:
    """Read the effective prompt text for a key (override or bundled default).

    Raises:
        FileNotFoundError: If neither an override nor a bundled file exists.
    """
    path = resolve_prompt_file(rel_key)
    if not path.is_file():
        raise FileNotFoundError(f"Prompt file not found: {path} (key={rel_key!r})")
    return path.read_text(encoding="utf-8")
