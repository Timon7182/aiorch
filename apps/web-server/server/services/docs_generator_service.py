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
        # CodeGraphContext (CGC) auto-indexing state, keyed by resolved project
        # path so two triggers for the same repo (e.g. the on-create hook and
        # the startup sweep) don't index it concurrently. The semaphore caps
        # how many repos index at once so a startup sweep over many projects
        # doesn't peg the CPU. Override the cap with CGC_INDEX_CONCURRENCY.
        self._cgc_indexing: set[str] = set()
        try:
            _cap = int(os.environ.get("CGC_INDEX_CONCURRENCY", "2") or "2")
        except ValueError:
            _cap = 2
        self._cgc_sema = asyncio.Semaphore(max(1, _cap))

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
        template: str | None = None,
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
            template: optional doc template name (structure/mkdocs/page
                layout). Defaults to "default" when None. Passed through to
                the runner's ``--template`` flag.
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
        if template:
            cmd += ["--template", template]

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

        return (output, ok)

    async def refresh_graph(self, project_path: Path) -> None:
        """Run `graphify extract` against the project (manual-trigger only).

        Writes graphify-out/{graph.json, GRAPH_REPORT.md, graph.html} at the
        project root, following graphify's own convention. The graph picks
        up source code (via tree-sitter, no API), generated docs/, and
        anything under .magestic-ai/uploaded-docs/ or .magestic-ai/transcripts/.

        NOT called automatically. The user invokes graphify themselves
        (typically via the `/graphify` slash command inside Claude Code, or
        by SSH'ing and running `graphify extract <path>`). This method
        stays as a callable shim in case a future endpoint wants to
        trigger it from the web UI — the downstream consumers (Hermes,
        coder/planner MCP) read whatever graph happens to exist.

        Backend: `gemini`. graphify v0.8.16 does NOT support a `claude-cli`
        backend (despite the upstream README's example), so we cannot ride
        the Claude Max subscription. GEMINI_API_KEY is already deployed
        for Hermes, so reusing it here avoids a new credential.
        """
        bin_path = self._resolve_graphify_bin()
        if bin_path is None:
            logger.info(
                "[DocsGenerator] graphify CLI not installed; skipping graph "
                "refresh. Ensure `graphifyy[mcp]` is in requirements.txt."
            )
            return

        if not os.environ.get("GEMINI_API_KEY"):
            logger.info(
                "[DocsGenerator] GEMINI_API_KEY not set; skipping graphify "
                "refresh. Set it in /home/saya/.aiorch-secrets or pick a "
                "different backend in refresh_graph()."
            )
            return

        env = os.environ.copy()

        cmd = [
            bin_path,
            "extract",
            str(project_path),
            "--backend",
            "gemini",
            "--max-workers",
            "2",
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

    async def refresh_codegraph(self, project_path: Path) -> None:
        """Run `codegraphcontext index <path>` against the project (manual trigger).

        Builds/refreshes the CodeGraphContext embedded graph DB under the
        project's `.codegraphcontext/` folder. Once that folder exists, the
        planner/coder/QA agents automatically get the CGC MCP tools
        (find_code, analyze_code_relationships, find_dead_code, ...) wired into
        their sessions — see core/client.py and agents/tools_pkg/models.py,
        which gate the `codegraph` server on the folder's presence.

        NOT called automatically (mirrors refresh_graph). The canonical manual
        path is to SSH and run `codegraphcontext index <path>` yourself; this
        method stays as a callable shim so a future endpoint can trigger it
        from the web UI. CGC indexes via tree-sitter with no LLM, so it needs
        no API key and runs fully offline.
        """
        bin_path = self._resolve_codegraph_bin()
        if bin_path is None:
            logger.info(
                "[DocsGenerator] codegraphcontext CLI not installed; skipping "
                "code-graph refresh. Ensure `codegraphcontext` is in requirements.txt."
            )
            return

        env = os.environ.copy()

        cmd = [bin_path, "index", str(project_path), "--force"]
        logger.info(f"[DocsGenerator] Refreshing CodeGraphContext index: {' '.join(cmd)}")

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
            logger.warning("[DocsGenerator] codegraphcontext index timed out after 10m")
            return
        except OSError as e:
            logger.warning(f"[DocsGenerator] codegraphcontext spawn failed: {e}")
            return

        output = (stdout or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            logger.warning(
                f"[DocsGenerator] codegraphcontext exited with code {proc.returncode}. "
                f"Tail: {output[-1000:]}"
            )
            return

        # Stamp the marker so the UI can show "code-graph as of <ts>".
        try:
            marker = project_path / ".magestic-ai" / ".docgen.json"
            existing: dict = {}
            if marker.exists():
                try:
                    existing = json.loads(marker.read_text())
                except json.JSONDecodeError:
                    pass
            existing["last_codegraph"] = datetime.now().isoformat()
            marker.write_text(json.dumps(existing, indent=2))
        except OSError:
            pass

    @staticmethod
    def auto_index_enabled() -> bool:
        """Whether CGC auto-indexing runs (on project create + startup sweep).

        Off when CODEGRAPH_DISABLED=true (the same global kill-switch the agent
        MCP gating honors, see agents/tools_pkg/models.py) or when CGC_AUTO_INDEX
        is explicitly falsey. Defaults on. The on-demand /docs/codegraph/index
        endpoint ignores this flag — it's an explicit user action.
        """
        if str(os.environ.get("CODEGRAPH_DISABLED", "")).lower() == "true":
            return False
        return str(os.environ.get("CGC_AUTO_INDEX", "true")).lower() not in (
            "false",
            "0",
            "no",
        )

    def is_indexing(self, project_path: Path) -> bool:
        """True while a CGC index is in flight (or queued) for this project."""
        return str(Path(project_path).resolve()) in self._cgc_indexing

    async def index_codegraph(self, project_path: Path) -> None:
        """Concurrency-guarded wrapper around refresh_codegraph.

        Dedups concurrent triggers for the same repo and caps how many repos
        index at once (CGC_INDEX_CONCURRENCY, default 2). Safe to fire-and-
        forget: refresh_codegraph no-ops cleanly when the CLI isn't installed.
        """
        key = str(Path(project_path).resolve())
        if key in self._cgc_indexing:
            logger.info(f"[DocsGenerator] CGC index already running for {key}; skipping")
            return
        self._cgc_indexing.add(key)
        try:
            async with self._cgc_sema:
                await self.refresh_codegraph(project_path)
        finally:
            self._cgc_indexing.discard(key)

    async def sweep_index_missing(self, project_paths: list[Path]) -> None:
        """Index every project that has no `.codegraphcontext/` graph yet.

        Drives the startup sweep. Already-indexed projects are skipped so
        repeated restarts are cheap; the per-repo concurrency cap in
        index_codegraph throttles the fan-out. No-ops when auto-indexing is
        disabled.
        """
        if not self.auto_index_enabled():
            return
        scheduled = 0
        for p in project_paths:
            try:
                path = Path(p)
            except TypeError:
                continue
            if not path.is_dir():
                continue
            if (path / ".codegraphcontext").is_dir():
                continue  # already indexed
            asyncio.create_task(self.index_codegraph(path))
            scheduled += 1
        if scheduled:
            logger.info(
                f"[DocsGenerator] CGC startup sweep scheduled indexing for "
                f"{scheduled} project(s)"
            )

    async def codegraph_report(self, repo_path: Path, *, refresh: bool = False) -> str | None:
        """Return CodeGraphContext's CGC_REPORT.md for a repo, as markdown.

        Generated on demand via `codegraphcontext report` into the repo's
        `.codegraphcontext/CGC_REPORT.md` (keeping the repo root clean), then
        read back. Cached after first generation; pass refresh=True to rebuild.

        Returns None when the repo has no `.codegraphcontext/` graph yet (not
        indexed) or the CLI is unavailable and no cached report exists. Mirrors
        the graphify GRAPH_REPORT.md surface so the docs panel can reuse its
        markdown viewer.
        """
        cgc_dir = repo_path / ".codegraphcontext"
        if not cgc_dir.is_dir():
            return None  # project hasn't been indexed by CGC

        report_path = cgc_dir / "CGC_REPORT.md"
        if report_path.is_file() and not refresh:
            return report_path.read_text(encoding="utf-8", errors="replace")

        bin_path = self._resolve_codegraph_bin()
        if bin_path is None:
            # Can't (re)generate; serve a stale report if we have one.
            if report_path.is_file():
                return report_path.read_text(encoding="utf-8", errors="replace")
            return None

        env = os.environ.copy()
        cmd = [bin_path, "report", "--output", str(report_path)]
        logger.info(f"[DocsGenerator] Generating CGC report: {' '.join(cmd)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(repo_path),
                env=env,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except (asyncio.TimeoutError, OSError) as e:
            logger.warning(f"[DocsGenerator] codegraphcontext report failed: {e}")

        if report_path.is_file():
            return report_path.read_text(encoding="utf-8", errors="replace")
        return None

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

    def _resolve_codegraph_bin(self) -> str | None:
        """Find the `codegraphcontext` executable.

        Honors CODEGRAPH_BIN (explicit absolute path) first — used by the
        dockerized deploy, where CGC lives in a persisted venv on a data volume
        so it survives redeploys. Otherwise prefers the web-server venv, then a
        PATH/`cgc` lookup.
        """
        explicit = os.environ.get("CODEGRAPH_BIN")
        if explicit and Path(explicit).exists():
            return explicit
        scripts_dir = Path(sys.executable).parent
        for name in ("codegraphcontext", "codegraphcontext.exe", "cgc", "cgc.exe"):
            candidate = scripts_dir / name
            if candidate.exists():
                return str(candidate)
        return shutil.which("codegraphcontext") or shutil.which("cgc")


_singleton: DocsGeneratorService | None = None


def get_docs_generator_service(backend_path: Path) -> DocsGeneratorService:
    global _singleton
    if _singleton is None:
        _singleton = DocsGeneratorService(backend_path)
    return _singleton
