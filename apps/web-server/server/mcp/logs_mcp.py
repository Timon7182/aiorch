"""Read-only logs MCP server (stdio) for the insights chat.

Exposes a small set of read-only tools so the chat can investigate runtime
behavior without a free-form shell:

  - list_app_logs()                       -> available web-server log names
  - read_app_log(log_type, lines)         -> tail of a web-server log file
  - list_remote_logs()                    -> allowlisted logs per SSH server
  - tail_remote_log(server, log, lines)   -> tail an allowlisted remote log
  - docker_logs(container, lines)         -> `docker logs` for an allowlisted container

Spawned by the Claude provider as ``python -m server.mcp.logs_mcp`` with
PYTHONPATH pointed at the web-server root (see
``insights_providers.claude_provider._build_logs_mcp_config``). The sys.path
bootstrap below also lets it run as a plain script path. All output is capped
to ~50KB and everything is strictly read-only — there is intentionally no tool
that runs an arbitrary command.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Bootstrap: ensure the web-server root (which contains the ``server`` package)
# is importable whether we're launched via ``-m`` (PYTHONPATH set) or as a bare
# script path. server/mcp/logs_mcp.py -> server/mcp -> server -> web-server.
_WEB_ROOT = Path(__file__).resolve().parents[2]
if str(_WEB_ROOT) not in sys.path:
    sys.path.insert(0, str(_WEB_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server.logging_config import get_log_files  # noqa: E402
from server.services import ext_storage, ssh_service  # noqa: E402

# Hard cap on any tool's returned text so a huge log can't blow the context.
_MAX_OUTPUT = 50_000
_MAX_LINES = 1000

mcp = FastMCP("logs")


def _cap(text: str) -> str:
    """Truncate tool output to the byte-ish size cap, keeping the tail (most
    recent log lines are the most useful)."""
    if len(text) <= _MAX_OUTPUT:
        return text
    return "…[truncated]…\n" + text[-_MAX_OUTPUT:]


def _clamp_lines(lines: int) -> int:
    try:
        n = int(lines)
    except (TypeError, ValueError):
        return 200
    return max(1, min(n, _MAX_LINES))


@mcp.tool()
def list_app_logs() -> list[str]:
    """List the web-server's own log files that currently exist on disk."""
    return [name for name, path in get_log_files().items() if path and path.exists()]


@mcp.tool()
def read_app_log(log_type: str, lines: int = 200) -> str:
    """Return the last ``lines`` (max 1000) of a web-server log file.

    ``log_type`` must be one of the names from ``list_app_logs`` (server,
    errors, agent, frontend).
    """
    log_files = get_log_files()
    path = log_files.get(log_type)
    if not path:
        return f"unknown log_type {log_type!r}; available: {sorted(log_files.keys())}"
    if not path.exists():
        return f"log {log_type!r} does not exist yet"
    n = _clamp_lines(lines)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-n:] if len(all_lines) > n else all_lines
        return _cap("".join(tail))
    except OSError as exc:
        return f"failed to read {log_type!r}: {exc}"


@mcp.tool()
def list_remote_logs() -> list[dict]:
    """List configured SSH servers and the allowlisted log names available on
    each (from each server profile's ``logs`` whitelist)."""
    out: list[dict] = []
    try:
        servers = ext_storage.load("servers")
    except Exception as exc:  # pragma: no cover - storage shape varies
        return [{"error": f"failed to load servers: {exc}"}]
    for s in servers or []:
        out.append({
            "name": s.get("name"),
            "id": s.get("id"),
            "host": s.get("host"),
            "logs": sorted((s.get("logs") or {}).keys()),
        })
    return out


def _find_server(server: str) -> dict | None:
    """Resolve a server profile by name first, then by id."""
    try:
        servers = ext_storage.load("servers")
    except Exception:
        return None
    for s in servers or []:
        if s.get("name") == server:
            return s
    for s in servers or []:
        if s.get("id") == server:
            return s
    return None


@mcp.tool()
def tail_remote_log(server: str, log_name: str, lines: int = 200) -> str:
    """Tail an allowlisted log on a configured SSH server.

    ``server`` matches a server profile name or id; ``log_name`` must be a key
    in that profile's ``logs`` allowlist. The allowlist is enforced in
    ssh_service.tail_log — arbitrary paths are rejected.
    """
    profile = _find_server(server)
    if not profile:
        return f"unknown server {server!r}; call list_remote_logs first"
    n = _clamp_lines(lines)
    try:
        res = ssh_service.tail_log(profile, log_name, lines=n)
    except ssh_service.SshError as exc:
        return f"error: {exc}"
    body = res.stdout or ""
    if res.stderr:
        body += f"\n[stderr]\n{res.stderr}"
    return _cap(body)


@mcp.tool()
def docker_logs(container: str, lines: int = 200) -> str:
    """Return `docker logs --tail N <container>` for an allowlisted container.

    Only containers listed in the ``LOGS_MCP_DOCKER_ALLOWLIST`` env var
    (comma-separated) are permitted. When the allowlist is empty this tool is
    disabled.
    """
    allow = [c.strip() for c in os.environ.get("LOGS_MCP_DOCKER_ALLOWLIST", "").split(",") if c.strip()]
    if not allow:
        return "docker_logs is disabled: set LOGS_MCP_DOCKER_ALLOWLIST (comma-separated container names)"
    if container not in allow:
        return f"container {container!r} is not in the allowlist ({', '.join(allow)})"
    n = _clamp_lines(lines)
    try:
        res = subprocess.run(
            ["docker", "logs", "--tail", str(n), container],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return "docker CLI not available on this host"
    except subprocess.SubprocessError as exc:
        return f"error running docker logs: {exc}"
    body = res.stdout or ""
    if res.stderr:
        # `docker logs` writes container stderr to our stderr; include it.
        body += f"\n[stderr]\n{res.stderr}"
    return _cap(body)


if __name__ == "__main__":
    mcp.run()
