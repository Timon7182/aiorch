"""
Tests for the per-project prompt resolver.

Verifies that resolve_prompt_file prefers a project's override directory
(MAGESTIC_PROMPT_OVERRIDE_DIR) when present and falls back to the bundled
defaults otherwise — the mechanism that makes per-project custom agent prompts
take effect in the backend subprocess.
"""

from pathlib import Path

import pytest

from prompts_pkg.prompt_resolver import (
    BUNDLED_PROMPTS_DIR,
    bundled_prompt_path,
    read_prompt,
    resolve_prompt_file,
)

ENV = "MAGESTIC_PROMPT_OVERRIDE_DIR"


@pytest.fixture
def override_dir(tmp_path, monkeypatch):
    """A populated override dir wired up via the env var."""
    d = tmp_path / "prompts"
    (d / "github").mkdir(parents=True)
    (d / "planner.md").write_text("CUSTOM PLANNER", encoding="utf-8")
    (d / "github" / "pr_reviewer.md").write_text("CUSTOM PR", encoding="utf-8")
    monkeypatch.setenv(ENV, str(d))
    return d


def test_falls_back_to_bundled_when_env_unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_prompt_file("planner.md") == BUNDLED_PROMPTS_DIR / "planner.md"


def test_override_wins_when_present(override_dir):
    assert resolve_prompt_file("planner.md") == override_dir / "planner.md"
    assert read_prompt("planner.md") == "CUSTOM PLANNER"


def test_override_supports_subdirectories(override_dir):
    assert resolve_prompt_file("github/pr_reviewer.md") == (
        override_dir / "github" / "pr_reviewer.md"
    )
    assert read_prompt("github/pr_reviewer.md") == "CUSTOM PR"


def test_unedited_key_falls_back_even_with_env_set(override_dir):
    # coder.md has no override file -> bundled default is used.
    assert resolve_prompt_file("coder.md") == BUNDLED_PROMPTS_DIR / "coder.md"


def test_insight_extractor_special_case_bundled_path():
    # Lives under analysis/prompts/, not prompts/.
    p = bundled_prompt_path("analysis/insight_extractor.md")
    assert p.name == "insight_extractor.md"
    assert "analysis" in p.parts and p.parent.name == "prompts"


def test_read_prompt_raises_for_unknown_key(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(FileNotFoundError):
        read_prompt("definitely_not_a_real_prompt_xyz.md")
