"""Documentation generator service.

Spawns `apps/backend/runners/docs_runner.py` as a subprocess (same pattern as
agent_service for spec_runner / run.py). The runner uses the Claude Agent
SDK to drive the doc-generator prompt against the project, producing a
`docs/` tree + `mkdocs.yml` in the repo. After the runner exits, this
service runs `mkdocs build` to produce the static HTML site under
`<project>/.magestic-ai/docs-site/` for serving via the web UI.

Two key design choices:

1. Markdown lives **in the project repo**, not in MagesticAI's data dir.
   That means `git diff` shows doc changes, GitHub/GitLab/Bitbucket render
   the markdown natively, and the next coding agent reads the docs as
   regular files (no MCP needed).
2. Built HTML lives in `.magestic-ai/docs-site/` (gitignored). Cheap to
   rebuild; never the source of truth.

Auth note: we deliberately do NOT shell out to the `claude` CLI directly.
The CLI's OAuth handling differs from the Python SDK and conflicts with how
MagesticAI passes credentials (env var only, no `~/.claude/.credentials.json`
inside the container). Spawning the same SDK-based runner that the rest of
the platform uses keeps auth and tooling consistent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GenerationResult:
    """Result of a documentation generation run."""

    success: bool
    project_id: str
    docs_dir: Path
    site_dir: Path
    files_generated: list[str]
    build_log: str
    error: str | None = None


class DocsGeneratorService:
    """Runs the doc-generator agent and builds the MkDocs site."""

    def __init__(self, backend_path: Path):
        # backend_path points at apps/backend; the runner + prompt live there.
        self.backend_path = backend_path
        self.runner_path = backend_path / "runners" / "docs_runner.py"
        # Track currently-running generations keyed by project_id so we don't
        # spawn two at once for the same project.
        self._running: dict[str, asyncio.subprocess.Process] = {}
        # Set of project_ids the route has accepted but whose subprocess
        # hasn't been spawned yet. Lets /status reflect "running" in the
        # window between accept and spawn so the UI doesn't show idle.
        self._starting: set[str] = set()
        # Project IDs we deliberately cancelled — used to suppress the
        # "exit code != 0" error in the post-run result.
        self._cancelled: set[str] = set()

    def is_running(self, project_id: str) -> bool:
        if project_id in self._starting:
            return True
        proc = self._running.get(project_id)
        return proc is not None and proc.returncode is None

    def mark_starting(self, project_id: str) -> None:
        """Synchronously mark a project as starting.

        Called by the route handler before the background task spawns the
        subprocess, so a /status call that races the spawn still sees
        `running`.
        """
        self._starting.add(project_id)

    async def cancel(self, project_id: str) -> bool:
        """Terminate an in-flight generation. Returns True if something
        was actually running (or queued)."""
        was_starting = project_id in self._starting
        self._starting.discard(project_id)
        proc = self._running.get(project_id)
        if proc is None or proc.returncode is not None:
            self._running.pop(project_id, None)
            return was_starting
        self._cancelled.add(project_id)
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        finally:
            self._running.pop(project_id, None)
        return True

    async def generate(
        self,
        project_id: str,
        project_path: Path,
        oauth_token: str | None,
        user_identity: tuple[str, str] | None = None,
    ) -> GenerationResult:
        """Run the doc-generator agent + mkdocs build for a project.

        Args:
            project_id: registered project ID (only used for tracking).
            project_path: absolute path to the project on disk.
            oauth_token: Claude Code OAuth token to authorize the agent. If
                None, the subprocess inherits whatever is in the environment.
            user_identity: optional (name, email) used to attribute any
                writes the agent makes via git (the agent itself doesn't
                commit, but stays consistent with the rest of the platform).
        """
        # Only reject a duplicate run if there's an *actual* subprocess
        # already running. The route handler may have called mark_starting()
        # for this same task, so the _starting flag on its own is fine.
        existing = self._running.get(project_id)
        if existing is not None and existing.returncode is None:
            return GenerationResult(
                success=False,
                project_id=project_id,
                docs_dir=project_path / "docs",
                site_dir=project_path / ".magestic-ai" / "docs-site",
                files_generated=[],
                build_log="",
                error="A documentation generation is already in progress for this project.",
            )

        # Idempotent: route may have already called mark_starting().
        self._starting.add(project_id)
        self._cancelled.discard(project_id)

        if not self.runner_path.exists():
            self._starting.discard(project_id)
            return GenerationResult(
                success=False,
                project_id=project_id,
                docs_dir=project_path / "docs",
                site_dir=project_path / ".magestic-ai" / "docs-site",
                files_generated=[],
                build_log="",
                error=f"Doc runner missing at {self.runner_path}",
            )

        env = self._build_env(oauth_token, user_identity)

        # Spawn the SDK-based runner. Same auth path as the rest of the
        # platform (env-var OAuth token; permissions handled in code, no
        # interactive prompts).
        cmd = [
            sys.executable,
            "-u",  # unbuffered, so we can stream progress to logs
            str(self.runner_path),
            "--project-dir",
            str(project_path),
        ]

        logger.info(
            f"[DocsGenerator] Starting runner for project {project_id} "
            f"({' '.join(cmd[1:])})"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.backend_path),
                env=env,
            )
            self._running[project_id] = proc
            # Subprocess is live; the _running entry is now the source of
            # truth for is_running().
            self._starting.discard(project_id)
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=900,  # 15 min cap on the generation phase
                )
            except asyncio.TimeoutError:
                proc.kill()
                return GenerationResult(
                    success=False,
                    project_id=project_id,
                    docs_dir=project_path / "docs",
                    site_dir=project_path / ".magestic-ai" / "docs-site",
                    files_generated=[],
                    build_log="",
                    error="Doc generator agent timed out after 15 minutes.",
                )
        finally:
            self._running.pop(project_id, None)
            self._starting.discard(project_id)

        runner_output = stdout.decode("utf-8", "replace") if stdout else ""
        # Cap stored output so we don't bloat the response.
        capped_log = runner_output[-4000:]

        was_cancelled = project_id in self._cancelled
        self._cancelled.discard(project_id)

        if was_cancelled:
            return GenerationResult(
                success=False,
                project_id=project_id,
                docs_dir=project_path / "docs",
                site_dir=project_path / ".magestic-ai" / "docs-site",
                files_generated=[],
                build_log=capped_log,
                error="Documentation generation was cancelled.",
            )

        if proc.returncode != 0:
            return GenerationResult(
                success=False,
                project_id=project_id,
                docs_dir=project_path / "docs",
                site_dir=project_path / ".magestic-ai" / "docs-site",
                files_generated=[],
                build_log=capped_log,
                error=f"Doc generator runner exited with code {proc.returncode}",
            )

        files_generated = self._enumerate_generated_files(project_path)
        # Make sure the marker file reflects this run even if the agent
        # didn't get to step 8 of the prompt.
        self._write_run_marker(project_path)
        build_log, build_ok = await self._build_site(project_path)

        if not build_ok:
            return GenerationResult(
                success=False,
                project_id=project_id,
                docs_dir=project_path / "docs",
                site_dir=project_path / ".magestic-ai" / "docs-site",
                files_generated=files_generated,
                build_log=build_log,
                error="`mkdocs build` failed — see build_log for details.",
            )

        return GenerationResult(
            success=True,
            project_id=project_id,
            docs_dir=project_path / "docs",
            site_dir=project_path / ".magestic-ai" / "docs-site",
            files_generated=files_generated,
            build_log=build_log,
            error=None,
        )

    async def build_only(self, project_path: Path) -> tuple[str, bool]:
        """Re-run `mkdocs build` without invoking the agent.

        Useful when the user has hand-edited markdown and wants to refresh
        the HTML site without burning agent tokens.
        """
        return await self._build_site(project_path)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _build_env(
        self,
        oauth_token: str | None,
        user_identity: tuple[str, str] | None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        env["CI"] = "true"
        if oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
        if user_identity:
            name, email = user_identity
            env["GIT_AUTHOR_NAME"] = name
            env["GIT_AUTHOR_EMAIL"] = email
            env["GIT_COMMITTER_NAME"] = name
            env["GIT_COMMITTER_EMAIL"] = email
        return env

    def _write_run_marker(self, project_path: Path) -> None:
        """Write/update .magestic-ai/.docgen.json with the current run metadata.

        The agent prompt also instructs the agent to update this file, but
        we write it from the service too so `last_run` is always reliable
        even when the agent skips step 8.
        """
        marker = project_path / ".magestic-ai" / ".docgen.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if marker.exists():
            try:
                existing = json.loads(marker.read_text())
            except json.JSONDecodeError:
                pass
        existing["last_run"] = datetime.now().isoformat()
        head_sha = self._git_head(project_path)
        if head_sha:
            existing["head_sha"] = head_sha
        try:
            marker.write_text(json.dumps(existing, indent=2))
        except OSError:
            pass

    def _git_head(self, project_path: Path) -> str | None:
        """Best-effort `git rev-parse --short HEAD`, returns None on failure."""
        import subprocess
        try:
            out = subprocess.check_output(
                ["git", "-c", f"safe.directory={project_path}",
                 "-C", str(project_path), "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return out.decode("utf-8", "replace").strip() or None
        except Exception:
            return None

    def _enumerate_generated_files(self, project_path: Path) -> list[str]:
        out: list[str] = []
        mkdocs_yml = project_path / "mkdocs.yml"
        if mkdocs_yml.exists():
            out.append("mkdocs.yml")
        docs_dir = project_path / "docs"
        if docs_dir.is_dir():
            for f in sorted(docs_dir.rglob("*.md")):
                out.append(str(f.relative_to(project_path)).replace("\\", "/"))
        return out

    async def _build_site(self, project_path: Path) -> tuple[str, bool]:
        """Run `mkdocs build -d <project>/.magestic-ai/docs-site/`.

        Returns (combined_output, success). The site dir is wiped first so
        stale pages don't linger after the user removes a markdown file.
        """
        mkdocs_yml = project_path / "mkdocs.yml"
        if not mkdocs_yml.exists():
            return ("mkdocs.yml not found at project root — skipping build", False)

        site_dir = project_path / ".magestic-ai" / "docs-site"
        if site_dir.exists():
            shutil.rmtree(site_dir, ignore_errors=True)
        site_dir.mkdir(parents=True, exist_ok=True)

        # Use the venv's mkdocs if available; otherwise fall back to PATH.
        mkdocs_bin = self._resolve_mkdocs_bin()
        if mkdocs_bin is None:
            return (
                "mkdocs not installed in container — add `mkdocs-material` to "
                "apps/web-server/requirements.txt and rebuild.",
                False,
            )

        proc = await asyncio.create_subprocess_exec(
            mkdocs_bin,
            "build",
            "--clean",
            "--site-dir",
            str(site_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(project_path),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return ("mkdocs build timed out after 2 minutes", False)

        output = stdout.decode("utf-8", "replace")
        ok = proc.returncode == 0

        # Persist a small marker file so the UI can read last-build metadata.
        try:
            marker = project_path / ".magestic-ai" / ".docgen.json"
            existing: dict = {}
            if marker.exists():
                try:
                    existing = json.loads(marker.read_text())
                except json.JSONDecodeError:
                    pass
            existing["last_build"] = datetime.now().isoformat()
            existing["last_build_ok"] = ok
            marker.write_text(json.dumps(existing, indent=2))
        except OSError:
            pass

        # After a successful mkdocs build, fold the docs + repo into the
        # graphify knowledge graph. Graphify is the single source Hermes
        # and the coder/planner all query, so keeping it fresh on every
        # docs run means uploaded transcripts and regenerated docs both
        # flow into the same graph the agents read. Non-fatal: docs
        # generation already succeeded by this point.
        if ok:
            await self.refresh_graph(project_path)

        return (output, ok)

    async def refresh_graph(self, project_path: Path) -> None:
        """Run `graphify extract --update` against the project (best-effort).

        Writes graphify-out/{graph.json, GRAPH_REPORT.md, graph.html} at the
        project root, following graphify's own convention. The graph picks
        up source code (via tree-sitter, no API), generated docs/, and
        anything under .magestic-ai/uploaded-docs/ or .magestic-ai/transcripts/.

        Auth path: `--backend claude-cli` routes through the user's Claude
        Code CLI, which means graphify rides on the existing Claude Max
        subscription. Recent deploy work persists ~/.claude/ across
        restarts (named volume) and refreshes the OAuth token in the
        background, so the CLI is authenticated by the time we get here.
        No ANTHROPIC_API_KEY required.

        If the claude binary isn't on PATH (or the user hasn't completed
        OAuth setup yet), graphify will error out; we log and continue —
        docs generation already succeeded by this point.
        """
        bin_path = self._resolve_graphify_bin()
        if bin_path is None:
            logger.info(
                "[DocsGenerator] graphify CLI not installed; skipping graph "
                "refresh. Ensure `graphifyy[mcp]` is in requirements.txt."
            )
            return

        # We deliberately do NOT strip CLAUDE_CODE_OAUTH_TOKEN here — the
        # `claude` CLI graphify shells out to reads it (along with
        # ~/.claude/.credentials.json) to authenticate against the user's
        # subscription. The "strip OAuth env vars" pattern in this codebase
        # is only for the *refresh* code path (commit e1ee23f), not for
        # normal CLI invocations like this one.
        env = os.environ.copy()

        cmd = [
            bin_path,
            "extract",
            str(project_path),
            "--backend",
            "claude-cli",
            "--update",
        ]
        logger.info(f"[DocsGenerator] Refreshing graphify graph: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_path),
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            logger.warning("[DocsGenerator] graphify refresh timed out after 10m")
            return
        except OSError as e:
            logger.warning(f"[DocsGenerator] graphify spawn failed: {e}")
            return

        output = (stdout or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            logger.warning(
                f"[DocsGenerator] graphify exited with code {proc.returncode}. "
                f"Tail: {output[-1000:]}"
            )
            return

        # Stamp the marker so the UI can show "graph as of <ts>".
        try:
            marker = project_path / ".magestic-ai" / ".docgen.json"
            existing: dict = {}
            if marker.exists():
                try:
                    existing = json.loads(marker.read_text())
                except json.JSONDecodeError:
                    pass
            existing["last_graphify"] = datetime.now().isoformat()
            marker.write_text(json.dumps(existing, indent=2))
        except OSError:
            pass

    def _resolve_mkdocs_bin(self) -> str | None:
        """Find a usable `mkdocs` executable, preferring the venv."""
        # Same venv that runs the web server.
        venv_bin = Path(sys.executable).parent / "mkdocs"
        if venv_bin.exists():
            return str(venv_bin)
        which = shutil.which("mkdocs")
        return which

    def _resolve_graphify_bin(self) -> str | None:
        """Find the `graphify` executable, preferring the web-server venv."""
        venv_bin = Path(sys.executable).parent / "graphify"
        if venv_bin.exists():
            return str(venv_bin)
        return shutil.which("graphify")


_singleton: DocsGeneratorService | None = None


def get_docs_generator_service(backend_path: Path) -> DocsGeneratorService:
    global _singleton
    if _singleton is None:
        _singleton = DocsGeneratorService(backend_path)
    return _singleton
