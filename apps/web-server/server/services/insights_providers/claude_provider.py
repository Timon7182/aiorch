"""
Claude CLI provider for insights chat.

Extracted from InsightsService — runs `claude --print --output-format stream-json`.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ...websockets.events import broadcast_event
from ..usage_recorder import record_project_usage
from .base import ProviderInfo, ProviderModel, ProviderStrategy

logger = logging.getLogger(__name__)

# Claude models (static — CLI supports these shorthands)
CLAUDE_MODELS = [
    ProviderModel(id="opus", label="Claude Opus 4.7"),
    ProviderModel(id="sonnet", label="Claude Sonnet 4.6"),
    ProviderModel(id="haiku", label="Claude Haiku 4.5"),
]

# System-prompt nudge appended when the CodeGraph backend is active so the model
# reaches for the graph tools instead of grepping the tree by hand.
CODEGRAPH_SYSTEM_PROMPT = (
    "This project is indexed in CodeGraph and you have its MCP tools available: "
    "mcp__codegraph__find_code, mcp__codegraph__analyze_code_relationships, "
    "mcp__codegraph__find_dead_code, mcp__codegraph__calculate_cyclomatic_complexity, "
    "mcp__codegraph__find_most_complex_functions, mcp__codegraph__execute_cypher_query, "
    "mcp__codegraph__list_indexed_repositories, mcp__codegraph__get_repository_stats. "
    "Prefer these tools over raw Grep/Glob for questions about where code is defined, "
    "what calls what, call chains, symbol relationships, dead code, or complexity. "
    "Fall back to Read/Grep/Glob only for plain-text search or reading file contents."
)


def resolve_codegraph_bin() -> str | None:
    """Find the `codegraphcontext` CLI as an absolute path.

    Mirrors core.client._resolve_codegraph_bin so the web-server process
    doesn't have to import the backend package:
      1. CODEGRAPH_BIN env var (explicit absolute path).
      2. The scripts/bin dir of the running interpreter.
      3. A plain PATH lookup (e.g. a pipx shim).
    """
    import sys

    explicit = os.environ.get("CODEGRAPH_BIN")
    if explicit and Path(explicit).exists():
        return explicit

    scripts_dir = Path(sys.executable).parent
    for name in ("codegraphcontext", "codegraphcontext.exe", "cgc", "cgc.exe"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)
    return shutil.which("codegraphcontext") or shutil.which("cgc")


def codegraph_available(run_dir: Path) -> bool:
    """CGC is usable only when enabled, indexed, and the CLI is installed.

    Shared by the provider (to decide whether to inject the MCP server) and the
    availability route (to tell the UI whether to offer CodeGraph for the dir
    the chat will actually run in).
    """
    if str(os.environ.get("CODEGRAPH_DISABLED", "")).lower() == "true":
        return False
    if not (run_dir / ".codegraphcontext").is_dir():
        return False
    return resolve_codegraph_bin() is not None


class ClaudeProvider(ProviderStrategy):
    """Provider that shells out to the Claude Code CLI."""

    def __init__(self) -> None:
        self._claude_path: str | None = None

    # ------------------------------------------------------------------
    # Detection (reuses InsightsService._resolve_claude_path logic)
    # ------------------------------------------------------------------

    def _resolve_claude_path(self) -> str:
        if self._claude_path:
            return self._claude_path

        path = shutil.which("claude")
        if path:
            self._claude_path = path
            return path

        try:
            result = subprocess.run(
                ["bash", "-l", "-c", "which claude"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                self._claude_path = result.stdout.strip()
                return self._claude_path
        except (subprocess.SubprocessError, OSError):
            pass

        home = Path.home()
        for candidate in [
            home / ".local" / "bin" / "claude",
            Path("/usr/local/bin/claude"),
        ]:
            if candidate.exists():
                self._claude_path = str(candidate)
                return self._claude_path

        return "claude"

    def _resolve_claude_token(self) -> tuple[str | None, str | None, str | None]:
        from ...config import get_settings
        settings = get_settings()

        env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if env_token:
            return (env_token, "env-override", "Environment Override")

        profiles_file = Path(settings.PROJECTS_DATA_DIR) / "claude-profiles.json"
        from ...paths import get_data_file
        legacy_profiles_file = get_data_file("claude-profiles.json")
        if not profiles_file.exists() and legacy_profiles_file.exists():
            profiles_file = legacy_profiles_file

        if profiles_file.exists():
            try:
                data = json.loads(profiles_file.read_text())
                profiles = data.get("profiles", [])
                active_id = data.get("activeProfileId")
                usable = [p for p in profiles if p.get("oauthToken") or p.get("token")]

                for profile in usable:
                    if profile.get("id") == active_id:
                        token = profile.get("oauthToken") or profile.get("token")
                        return (token, profile.get("id"), profile.get("name", "Active Profile"))

                if usable:
                    profile = usable[0]
                    token = profile.get("oauthToken") or profile.get("token")
                    return (token, profile.get("id"), profile.get("name", "Default Profile"))
            except (json.JSONDecodeError, OSError):
                pass

        token_file = Path.home() / ".claude" / "oauth_token"
        if token_file.exists():
            token = token_file.read_text().strip()
            if token:
                return (token, "static-fallback", "Static Token")

        return (None, None, None)

    async def detect(self) -> ProviderInfo:
        claude_bin = self._resolve_claude_path()
        available = shutil.which(claude_bin) is not None or claude_bin != "claude"

        token, _, profile_name = self._resolve_claude_token()
        auth = None
        if token:
            auth = f"OAuth ({profile_name})" if profile_name else "OAuth"

        return ProviderInfo(
            provider="claude",
            available=available and token is not None,
            display_name="Claude",
            icon="sparkles",
            auth_method=auth,
            models=CLAUDE_MODELS,
        )

    # ------------------------------------------------------------------
    # CodeGraph (CGC) backend — inject the codegraph stdio MCP server so
    # Claude can answer structural questions via the indexed graph.
    # ------------------------------------------------------------------

    def _resolve_code_search_mode(self, code_search: str | None, run_dir: Path) -> str:
        """Resolve the requested backend to a concrete one.

        'cgc'/'files' are honored as-is; 'auto' (the default) prefers CodeGraph
        when the project is indexed, otherwise falls back to plain file tools.
        """
        if code_search in ("cgc", "files"):
            return code_search
        return "cgc" if codegraph_available(run_dir) else "files"

    def _build_codegraph_mcp_config(self, run_dir: Path) -> dict | None:
        """Build the inline --mcp-config payload for the codegraph stdio server."""
        if not codegraph_available(run_dir):
            return None
        cgc_bin = resolve_codegraph_bin()
        if not cgc_bin:
            return None
        return {"mcpServers": {"codegraph": {"command": cgc_bin, "args": ["mcp", "start"]}}}

    # ------------------------------------------------------------------
    # Message sending (extracted from InsightsService.send_message)
    # ------------------------------------------------------------------

    async def send_message(
        self,
        project_path: Path,
        project_id: str,
        message: str,
        model: str | None,
        model_config: dict | None,
        conversation_history: list[dict] | None,
        working_dir: Path | None = None,
        attachment_dir: Path | None = None,
    ) -> str:
        # Run the CLI in the branch worktree when one was selected; usage and
        # token resolution below still key off the main project_path.
        run_dir = working_dir or project_path
        claude_bin = self._resolve_claude_path()
        cmd = [
            claude_bin,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
        ]

        if model_config:
            model_value = model_config.get("model") or model
            if model_value:
                cmd.extend(["--model", model_value])

            thinking_level = model_config.get("thinkingLevel")
            if thinking_level and thinking_level != "none":
                effort_map = {"low": "low", "medium": "medium", "high": "high"}
                effort = effort_map.get(thinking_level)
                if effort:
                    cmd.extend(["--effort", effort])
        elif model:
            cmd.extend(["--model", model])

        # Code-search backend: optionally inject the CodeGraph MCP server so the
        # model navigates the indexed graph instead of grepping raw files.
        code_search = (model_config or {}).get("codeSearch") or "auto"
        search_mode = self._resolve_code_search_mode(code_search, run_dir)
        if search_mode == "cgc":
            cgc_config = self._build_codegraph_mcp_config(run_dir)
            if cgc_config:
                # NOTE: --allowedTools is variadic, so it must be followed by
                # another flag (here --append-system-prompt) — never by the
                # trailing positional message, which it would otherwise swallow.
                cmd.extend([
                    "--mcp-config", json.dumps(cgc_config),
                    "--strict-mcp-config",
                    "--allowedTools", "mcp__codegraph__*", "Read", "Glob", "Grep",
                    "--append-system-prompt", CODEGRAPH_SYSTEM_PROMPT,
                ])
                logger.info("[ClaudeProvider] CodeGraph MCP enabled for this turn")
            else:
                logger.info(
                    "[ClaudeProvider] CodeGraph requested but unavailable "
                    "(not indexed / disabled / CLI missing); using file tools"
                )

        # Grant the CLI read access to attachment files that live outside the
        # run dir (e.g. images written under the project's .magestic-ai while the
        # chat runs in a branch worktree). The `=` form assigns a single value so
        # this array-typed flag doesn't greedily swallow the positional message.
        if attachment_dir is not None:
            cmd.append(f"--add-dir={attachment_dir}")

        cmd.append(message)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("CLAUDECODE", None)

        token, profile_id, profile_name = self._resolve_claude_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            logger.info(f"[ClaudeProvider] Using profile: {profile_name} ({profile_id})")
        else:
            logger.warning("[ClaudeProvider] No OAuth token available")

        logger.info(f"[ClaudeProvider] Starting CLI: {' '.join(cmd[:5])}...")

        try:
            await broadcast_event("insights:chunk", {
                "projectId": project_id,
                "type": "text",
                "content": "",
            })

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(run_dir),
                env=env,
                limit=10 * 1024 * 1024,
            )

            accumulated_content = ""
            tools_used = []
            stream_start = time.monotonic()

            async for line_bytes in proc.stdout:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        event_type = data.get("type", "")

                        if event_type == "assistant":
                            content = data.get("message", {}).get("content", "")
                            if isinstance(content, list):
                                for block in content:
                                    if block.get("type") == "text":
                                        text = block.get("text", "")
                                        accumulated_content += text
                                        await broadcast_event("insights:chunk", {
                                            "projectId": project_id,
                                            "type": "text",
                                            "content": text,
                                        })
                            elif isinstance(content, str):
                                accumulated_content += content
                                await broadcast_event("insights:chunk", {
                                    "projectId": project_id,
                                    "type": "text",
                                    "content": content,
                                })

                        elif event_type == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                accumulated_content += text
                                await broadcast_event("insights:chunk", {
                                    "projectId": project_id,
                                    "type": "text",
                                    "content": text,
                                })

                        elif event_type == "tool_use":
                            tool_name = data.get("name", data.get("tool", "Unknown"))
                            tool_input = data.get("input", "")
                            if isinstance(tool_input, dict):
                                tool_input = tool_input.get("file_path") or tool_input.get("pattern") or str(tool_input)[:100]
                            tools_used.append({
                                "name": tool_name,
                                "input": str(tool_input)[:200],
                                "timestamp": datetime.now().isoformat(),
                            })
                            await broadcast_event("insights:chunk", {
                                "projectId": project_id,
                                "type": "tool_start",
                                "tool": {"name": tool_name, "input": str(tool_input)[:200]},
                            })

                        elif event_type == "tool_result":
                            await broadcast_event("insights:chunk", {
                                "projectId": project_id,
                                "type": "tool_end",
                            })

                        elif event_type == "result":
                            result = data.get("result", "")
                            if result and result != accumulated_content:
                                accumulated_content = result
                                await broadcast_event("insights:chunk", {
                                    "projectId": project_id,
                                    "type": "text",
                                    "content": result,
                                })
                            # The CLI's `result` event carries the canonical
                            # per-turn token totals + SDK-computed cost.
                            # Record exactly once per send_message() call.
                            cli_usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
                            if cli_usage:
                                cli_cost = data.get("total_cost_usd")
                                cli_model = data.get("model") or (model_config.get("model") if model_config else model)
                                record_project_usage(
                                    project_path=project_path,
                                    project_id=project_id,
                                    feature="insights",
                                    phase="chat",
                                    model=cli_model,
                                    input_tokens=int(cli_usage.get("input_tokens", 0) or 0),
                                    output_tokens=int(cli_usage.get("output_tokens", 0) or 0),
                                    cache_read_input_tokens=int(
                                        cli_usage.get("cache_read_input_tokens", 0) or 0
                                    ),
                                    cache_creation_input_tokens=int(
                                        cli_usage.get("cache_creation_input_tokens", 0) or 0
                                    ),
                                    cost_usd=(
                                        float(cli_cost) if cli_cost is not None else None
                                    ),
                                )

                        continue
                    except json.JSONDecodeError:
                        pass

                accumulated_content += line + "\n"
                await broadcast_event("insights:chunk", {
                    "projectId": project_id,
                    "type": "text",
                    "content": line + "\n",
                })

            await proc.wait()

            stderr_output = await proc.stderr.read()
            stderr_text = ""
            if stderr_output:
                stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
                logger.warning(f"[ClaudeProvider] stderr: {stderr_text}")

            if proc.returncode != 0 and not accumulated_content.strip():
                error_msg = stderr_text or f"Claude CLI exited with code {proc.returncode}"
                logger.error(f"[ClaudeProvider] CLI failed: {error_msg}")
                await broadcast_event("insights:chunk", {
                    "projectId": project_id,
                    "type": "error",
                    "error": error_msg,
                })
                return ""

            elapsed = time.monotonic() - stream_start
            # Estimate tokens: ~4 chars per token for English text
            estimated_tokens = max(1, len(accumulated_content) // 4)
            tokens_per_sec = round(estimated_tokens / elapsed, 1) if elapsed > 0 else 0

            await broadcast_event("insights:chunk", {
                "projectId": project_id,
                "type": "done",
                "metrics": {
                    "outputTokens": estimated_tokens,
                    "tokensPerSecond": tokens_per_sec,
                    "elapsedSeconds": round(elapsed, 1),
                    "estimated": True,
                },
            })

            return accumulated_content

        except Exception as e:
            logger.error(f"[ClaudeProvider] Error: {e}", exc_info=True)
            await broadcast_event("insights:chunk", {
                "projectId": project_id,
                "type": "error",
                "error": str(e),
            })
            return ""
