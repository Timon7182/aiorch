#!/usr/bin/env python3
"""
Tests for Git Worktree Management
=================================

Tests the worktree.py module functionality including:
- Worktree creation and removal
- Staging worktree management
- Branch operations
- Merge operations
- Change tracking
"""

import json
import subprocess
from pathlib import Path

import pytest
from worktree import WorktreeError, WorktreeInfo, WorktreeManager


class TestWorktreeManagerInitialization:
    """Tests for WorktreeManager initialization."""

    def test_init_with_valid_git_repo(self, temp_git_repo: Path):
        """Manager initializes correctly with valid git repo."""
        manager = WorktreeManager(temp_git_repo)

        assert manager.project_dir == temp_git_repo
        assert manager.worktrees_dir == temp_git_repo / ".magestic-ai" / "worktrees" / "tasks"
        assert manager.base_branch is not None

    def test_init_prefers_main_over_current_branch(self, temp_git_repo: Path):
        """Manager prefers main/master over current branch when detecting base branch."""
        # Create and switch to a new branch
        subprocess.run(
            ["git", "checkout", "-b", "feature-branch"],
            cwd=temp_git_repo, capture_output=True
        )

        # Even though we're on feature-branch, manager should prefer main
        manager = WorktreeManager(temp_git_repo)
        assert manager.base_branch == "main"

    def test_init_falls_back_to_current_branch(self, temp_git_repo: Path):
        """Manager falls back to current branch when main/master don't exist."""
        # Delete main branch to force fallback
        subprocess.run(
            ["git", "checkout", "-b", "feature-branch"],
            cwd=temp_git_repo, capture_output=True
        )
        subprocess.run(
            ["git", "branch", "-D", "main"],
            cwd=temp_git_repo, capture_output=True
        )

        manager = WorktreeManager(temp_git_repo)
        assert manager.base_branch == "feature-branch"

    def test_init_with_explicit_base_branch(self, temp_git_repo: Path):
        """Manager uses explicitly provided base branch."""
        manager = WorktreeManager(temp_git_repo, base_branch="main")
        assert manager.base_branch == "main"

    def test_setup_creates_worktrees_directory(self, temp_git_repo: Path):
        """Setup creates the worktrees directory."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        assert manager.worktrees_dir.exists()
        assert manager.worktrees_dir.is_dir()


class TestWorktreeCreation:
    """Tests for creating worktrees."""

    def test_create_worktree(self, temp_git_repo: Path):
        """Can create a new worktree."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        info = manager.create_worktree("test-spec")

        assert info.path.exists()
        assert info.branch == "feature/test-spec"
        assert info.is_active is True
        assert (info.path / "README.md").exists()

    def test_create_worktree_with_spec_name(self, temp_git_repo: Path):
        """Worktree branch is derived from spec name."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        info = manager.create_worktree("my-feature-spec")

        assert info.branch == "feature/my-feature-spec"

    def test_get_or_create_replaces_existing_worktree(self, temp_git_repo: Path):
        """get_or_create_worktree returns existing worktree."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        info1 = manager.create_worktree("test-spec")
        # Create a file in the worktree
        (info1.path / "test-file.txt").write_text("test")

        # get_or_create should return existing
        info2 = manager.get_or_create_worktree("test-spec")

        assert info2.path.exists()
        # The test file should still be there (same worktree)
        assert (info2.path / "test-file.txt").exists()


class TestWorktreeRemoval:
    """Tests for removing worktrees."""

    def test_remove_worktree(self, temp_git_repo: Path):
        """Can remove a worktree."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        info = manager.create_worktree("test-spec")

        manager.remove_worktree("test-spec")

        assert not info.path.exists()

    def test_remove_with_delete_branch(self, temp_git_repo: Path):
        """Removing worktree can also delete the branch."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        info = manager.create_worktree("test-spec")
        branch_name = info.branch

        manager.remove_worktree("test-spec", delete_branch=True)

        # Verify branch is deleted
        result = subprocess.run(
            ["git", "branch", "--list", branch_name],
            cwd=temp_git_repo, capture_output=True, text=True
        )
        assert branch_name not in result.stdout


class TestWorktreeCommitAndMerge:
    """Tests for commit and merge operations."""

    def test_merge_worktree(self, temp_git_repo: Path):
        """Can merge a worktree back to main."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        # Create a worktree with changes
        worker_info = manager.create_worktree("worker-spec")
        (worker_info.path / "worker-file.txt").write_text("worker content")
        subprocess.run(["git", "add", "."], cwd=worker_info.path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Worker commit"],
            cwd=worker_info.path, capture_output=True
        )

        # Merge worktree back to main
        result = manager.merge_worktree("worker-spec", delete_after=False)

        assert result is True

        # Verify file is in main branch
        subprocess.run(["git", "checkout", manager.base_branch], cwd=temp_git_repo, capture_output=True)
        assert (temp_git_repo / "worker-file.txt").exists()


class TestChangeTracking:
    """Tests for tracking changes in worktrees."""

    def test_has_uncommitted_changes_false(self, temp_git_repo: Path):
        """has_uncommitted_changes returns False when clean."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        assert manager.has_uncommitted_changes() is False

    def test_has_uncommitted_changes_true(self, temp_git_repo: Path):
        """has_uncommitted_changes returns True when dirty."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        # Make uncommitted changes
        (temp_git_repo / "dirty.txt").write_text("uncommitted")

        assert manager.has_uncommitted_changes() is True

    def test_get_change_summary(self, temp_git_repo: Path):
        """get_change_summary returns correct counts."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        info = manager.create_worktree("test-spec")

        # Make various changes
        (info.path / "new-file.txt").write_text("new")
        (info.path / "README.md").write_text("modified")
        subprocess.run(["git", "add", "."], cwd=info.path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Changes"],
            cwd=info.path, capture_output=True
        )

        summary = manager.get_change_summary("test-spec")

        assert summary["new_files"] == 1  # new-file.txt
        assert summary["modified_files"] == 1  # README.md

    def test_get_changed_files(self, temp_git_repo: Path):
        """get_changed_files returns list of changed files."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        info = manager.create_worktree("test-spec")

        # Make changes
        (info.path / "added.txt").write_text("new file")
        subprocess.run(["git", "add", "."], cwd=info.path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add file"],
            cwd=info.path, capture_output=True
        )

        files = manager.get_changed_files("test-spec")

        assert len(files) > 0
        file_names = [f[1] for f in files]
        assert "added.txt" in file_names


class TestWorktreeUtilities:
    """Tests for utility methods."""

    def test_list_worktrees(self, temp_git_repo: Path):
        """list_all_worktrees returns active worktrees."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        manager.create_worktree("spec-1")
        manager.create_worktree("spec-2")

        worktrees = manager.list_all_worktrees()

        assert len(worktrees) == 2

    def test_get_info(self, temp_git_repo: Path):
        """get_worktree_info returns correct WorktreeInfo."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        manager.create_worktree("test-spec")

        info = manager.get_worktree_info("test-spec")

        assert info is not None
        assert info.branch == "feature/test-spec"

    def test_get_worktree_path(self, temp_git_repo: Path):
        """get_worktree_path returns correct path."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        info = manager.create_worktree("test-spec")

        path = manager.get_worktree_path("test-spec")

        assert path == info.path

    def test_cleanup_all(self, temp_git_repo: Path):
        """cleanup_all removes all worktrees."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        manager.create_worktree("spec-1")
        manager.create_worktree("spec-2")
        manager.create_worktree("spec-3")

        manager.cleanup_all()

        assert len(manager.list_all_worktrees()) == 0

    def test_cleanup_stale_worktrees(self, temp_git_repo: Path):
        """cleanup_stale_worktrees removes directories without git tracking."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        # Create a stale worktree directory (exists but not tracked by git)
        stale_dir = manager.worktrees_dir / "stale-worktree"
        stale_dir.mkdir(parents=True, exist_ok=True)

        # This should clean up the stale directory
        manager.cleanup_stale_worktrees()

        # Stale directory should be removed
        assert not stale_dir.exists()

    def test_get_test_commands_python(self, temp_git_repo: Path):
        """get_test_commands detects Python project commands."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        info = manager.create_worktree("test-spec")

        # Create requirements.txt
        (info.path / "requirements.txt").write_text("flask\n")

        commands = manager.get_test_commands("test-spec")

        assert any("pip" in cmd for cmd in commands)

    def test_get_test_commands_node(self, temp_git_repo: Path):
        """get_test_commands detects Node.js project commands."""
        manager = WorktreeManager(temp_git_repo)
        manager.setup()
        info = manager.create_worktree("test-spec-node")

        # Create package.json
        (info.path / "package.json").write_text('{"name": "test"}')

        commands = manager.get_test_commands("test-spec-node")

        assert any("npm" in cmd for cmd in commands)


class TestCustomBranchName:
    """Tests for user-supplied custom branch names."""

    def _write_spec_metadata(self, project_dir: Path, spec_name: str, key: str, payload: dict) -> None:
        """Write a spec metadata file (task_metadata.json or requirements.json)."""
        spec_dir = project_dir / ".magestic-ai" / "specs" / spec_name
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / key).write_text(json.dumps(payload), encoding="utf-8")

    def test_default_branch_when_no_metadata(self, temp_git_repo: Path):
        """Without metadata, branch falls back to feature/{spec}."""
        manager = WorktreeManager(temp_git_repo)
        assert manager.get_branch_name("my-spec") == "feature/my-spec"

    def test_custom_branch_from_task_metadata(self, temp_git_repo: Path):
        """customBranchName in task_metadata.json is honored verbatim."""
        self._write_spec_metadata(
            temp_git_repo, "my-spec", "task_metadata.json",
            {"customBranchName": "hotfix/32_task"},
        )
        manager = WorktreeManager(temp_git_repo)
        assert manager.get_branch_name("my-spec") == "hotfix/32_task"

    def test_custom_branch_from_requirements_fallback(self, temp_git_repo: Path):
        """customBranchName in requirements.json[metadata] is used as fallback."""
        self._write_spec_metadata(
            temp_git_repo, "my-spec", "requirements.json",
            {"metadata": {"customBranchName": "release/v2"}},
        )
        manager = WorktreeManager(temp_git_repo)
        assert manager.get_branch_name("my-spec") == "release/v2"

    def test_task_metadata_takes_precedence(self, temp_git_repo: Path):
        """task_metadata.json wins over requirements.json."""
        self._write_spec_metadata(
            temp_git_repo, "my-spec", "task_metadata.json",
            {"customBranchName": "hotfix/win"},
        )
        self._write_spec_metadata(
            temp_git_repo, "my-spec", "requirements.json",
            {"metadata": {"customBranchName": "release/lose"}},
        )
        manager = WorktreeManager(temp_git_repo)
        assert manager.get_branch_name("my-spec") == "hotfix/win"

    def test_custom_branch_is_sanitized(self, temp_git_repo: Path):
        """Whitespace becomes hyphens and forbidden chars are stripped."""
        self._write_spec_metadata(
            temp_git_repo, "my-spec", "task_metadata.json",
            {"customBranchName": "  hot fix/my task?  "},
        )
        manager = WorktreeManager(temp_git_repo)
        assert manager.get_branch_name("my-spec") == "hot-fix/my-task"

    @pytest.mark.parametrize("bad", ["", "   ", "..", "foo..bar", "/", "feature/.git", "x.lock"])
    def test_invalid_custom_branch_falls_back(self, temp_git_repo: Path, bad: str):
        """Invalid names fall back to the default namespace."""
        self._write_spec_metadata(
            temp_git_repo, "my-spec", "task_metadata.json",
            {"customBranchName": bad},
        )
        manager = WorktreeManager(temp_git_repo)
        assert manager.get_branch_name("my-spec") == "feature/my-spec"

    def test_create_worktree_uses_custom_branch(self, temp_git_repo: Path):
        """End to end: create_worktree checks out the custom branch."""
        self._write_spec_metadata(
            temp_git_repo, "my-spec", "task_metadata.json",
            {"customBranchName": "hotfix/32_task"},
        )
        manager = WorktreeManager(temp_git_repo)
        manager.setup()

        info = manager.create_worktree("my-spec")

        assert info.branch == "hotfix/32_task"
        # The worktree's actual HEAD is the custom branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=info.path, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "hotfix/32_task"
