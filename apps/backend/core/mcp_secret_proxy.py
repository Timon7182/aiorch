"""
MCP Secret-Substitution Proxy
=============================

A transparent stdio proxy placed between an MCP client (the Claude Code CLI)
and a stdio MCP server (e.g. ``npx @playwright/mcp``). Its single purpose is to
keep secrets (test-account passwords for UI checks) out of the LLM context:

- The agent types literal placeholders like ``${UI_CHECK_PASSWORD}`` into
  browser form fields. This proxy substitutes the real values (taken from its
  own environment) into client->server JSON-RPC messages, so the browser
  receives the real credentials while the model only ever saw the placeholder.
- In server->client messages the real values are redacted back to the
  placeholder, so page snapshots/echoes can never leak a secret into the
  model's context or into reports.

Usage:
    python mcp_secret_proxy.py -- <server command> [args...]

Environment:
    MCP_PROXY_SECRET_VARS   Comma-separated env var NAMES whose values are
                            secrets (e.g. "UI_CHECK_USERNAME,UI_CHECK_PASSWORD").
                            For each name VAR, the placeholder is ``${VAR}``.

The MCP stdio transport is newline-delimited JSON-RPC (UTF-8). Substitution is
JSON-aware: each line is parsed and only string values are rewritten, so
secrets containing quotes/backslashes survive JSON escaping correctly. Lines
that fail to parse are forwarded untouched (redacted textually as a
defense-in-depth measure on the server->client side).

This file is intentionally dependency-free and importable standalone (no
package-relative imports) so it can be launched from any cwd.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading


def load_secrets(environ: dict | None = None) -> dict[str, str]:
    """Map placeholder token -> real value from MCP_PROXY_SECRET_VARS."""
    env = environ if environ is not None else os.environ
    secrets: dict[str, str] = {}
    for name in str(env.get("MCP_PROXY_SECRET_VARS", "")).split(","):
        name = name.strip()
        if not name:
            continue
        value = env.get(name)
        if value:
            secrets["${%s}" % name] = value
    return secrets


def _walk_strings(obj, transform):
    """Recursively apply ``transform`` to every string in a JSON value."""
    if isinstance(obj, str):
        return transform(obj)
    if isinstance(obj, list):
        return [_walk_strings(item, transform) for item in obj]
    if isinstance(obj, dict):
        # Keys are tool-arg names etc. — never secrets; only rewrite values.
        return {key: _walk_strings(value, transform) for key, value in obj.items()}
    return obj


def substitute_line(line: str, secrets: dict[str, str]) -> str:
    """client->server: replace placeholders with real values (JSON-aware)."""
    if not secrets or not any(ph in line for ph in secrets):
        return line
    try:
        message = json.loads(line)
    except (ValueError, TypeError):
        return line

    def sub(s: str) -> str:
        for placeholder, value in secrets.items():
            s = s.replace(placeholder, value)
        return s

    return json.dumps(_walk_strings(message, sub), ensure_ascii=False)


def redact_line(line: str, secrets: dict[str, str]) -> str:
    """server->client: replace real values with placeholders.

    Tries JSON-aware replacement first; falls back to a textual pass over the
    raw line (covers both the parse-failure case and values that appear
    JSON-escaped) so a secret can never reach the model.
    """
    if not secrets:
        return line
    values_present = [v for v in secrets.values() if v in line]
    escaped_present = False
    for value in secrets.values():
        escaped = json.dumps(value)[1:-1]  # value as it appears inside a JSON string
        if escaped != value and escaped in line:
            escaped_present = True
    if not values_present and not escaped_present:
        return line
    try:
        message = json.loads(line)

        def red(s: str) -> str:
            for placeholder, value in secrets.items():
                s = s.replace(value, placeholder)
            return s

        return json.dumps(_walk_strings(message, red), ensure_ascii=False)
    except (ValueError, TypeError):
        for placeholder, value in secrets.items():
            line = line.replace(value, placeholder)
            escaped = json.dumps(value)[1:-1]
            if escaped != value:
                line = line.replace(escaped, placeholder)
        return line


def redact_bytes(raw: bytes, secrets: dict[str, str]) -> bytes:
    """Byte-level redaction fallback for non-UTF-8 chunks (server->client).

    The redaction guarantee must hold even for lines we cannot decode: replace
    UTF-8-encoded secret values with their placeholders directly in the bytes.
    """
    for placeholder, value in secrets.items():
        raw = raw.replace(value.encode("utf-8"), placeholder.encode("utf-8"))
    return raw


def _passthrough_bytes(raw: bytes, secrets: dict[str, str]) -> bytes:
    """client->server binary fallback: placeholders can't occur in binary, and
    substituting into undecodable data would corrupt it — forward untouched."""
    return raw


def _pump(src, dst, transform, secrets: dict[str, str], bytes_fallback) -> None:
    """Forward newline-delimited UTF-8 lines from src to dst through transform.

    Lines that fail UTF-8 decoding go through ``bytes_fallback`` instead, so
    the redaction guarantee holds on every path.
    """
    try:
        for raw in src:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                dst.write(bytes_fallback(raw, secrets))
                dst.flush()
                continue
            out = transform(text.rstrip("\r\n"), secrets)
            dst.write((out + "\n").encode("utf-8"))
            dst.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            dst.close()
        except (OSError, ValueError):
            pass


def main(argv: list[str]) -> int:
    if "--" in argv:
        command = argv[argv.index("--") + 1 :]
    else:
        command = argv
    if not command:
        print(
            "usage: mcp_secret_proxy.py -- <server command> [args...]",
            file=sys.stderr,
        )
        return 2

    # Resolve the executable (npx is npx.cmd on Windows; PATH lookup needed).
    resolved = shutil.which(command[0]) or command[0]
    secrets = load_secrets()

    child = subprocess.Popen(
        [resolved] + command[1:],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )

    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    stdout = getattr(sys.stdout, "buffer", sys.stdout)

    to_server = threading.Thread(
        target=_pump,
        args=(stdin, child.stdin, substitute_line, secrets, _passthrough_bytes),
        daemon=True,
    )
    to_client = threading.Thread(
        target=_pump,
        args=(child.stdout, stdout, redact_line, secrets, redact_bytes),
        daemon=True,
    )
    to_server.start()
    to_client.start()

    rc = child.wait()
    # Give the output pump a moment to drain remaining lines.
    to_client.join(timeout=5)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
