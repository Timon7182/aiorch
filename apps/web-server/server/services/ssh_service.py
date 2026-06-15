"""SSH service: allowlisted log-tail + deploy operations only.

Profile shape (no arbitrary command surface):
    {
      "id": str, "name": str, "host": str, "port": int,
      "username": str,
      "auth_method": "password" | "key",
      "password": str (optional),
      "private_key_path": str (optional),
      "logs": { "<name>": "<absolute-path>" },   # whitelist of tail targets
      "deploys": { "<name>": "<absolute-path-to-script>" },  # whitelist of deploy scripts
      "project": str | None
    }

There is intentionally NO endpoint that accepts a free-form shell command.
Operations are limited to:
  - test_connection: runs `echo MAGESTIC_OK && uname -a`
  - tail_log(name): runs `tail -n <N> <path>` where <path> comes from profile.logs[name]
  - run_deploy(name): runs the absolute script path stored in profile.deploys[name]

If you need to allowlist a new operation, add a new method here. Do not add
a generic exec endpoint.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

import paramiko


class SshError(RuntimeError):
    pass


@dataclass
class SshResult:
    stdout: str
    stderr: str
    exit_code: int


def _connect(profile: dict[str, Any]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict[str, Any] = {
        "hostname": profile["host"],
        "port": int(profile.get("port") or 22),
        "username": profile["username"],
        "timeout": int(profile.get("timeout") or 15),
        "look_for_keys": False,
        "allow_agent": False,
    }
    auth = profile.get("auth_method") or "password"
    if auth == "password":
        if not profile.get("password"):
            raise SshError("password auth selected but no password in profile")
        kwargs["password"] = profile["password"]
    elif auth == "key":
        if not profile.get("private_key_path"):
            raise SshError("key auth selected but no private_key_path in profile")
        kwargs["key_filename"] = profile["private_key_path"]
    else:
        raise SshError(f"unknown auth_method: {auth!r}")
    try:
        client.connect(**kwargs)
    except (paramiko.AuthenticationException, paramiko.SSHException, OSError) as exc:
        raise SshError(f"ssh connect failed: {exc}") from exc
    return client


def _run(profile: dict[str, Any], cmd: str, *, timeout: int) -> SshResult:
    client = _connect(profile)
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return SshResult(
            stdout=stdout.read().decode("utf-8", errors="replace"),
            stderr=stderr.read().decode("utf-8", errors="replace"),
            exit_code=exit_code,
        )
    finally:
        client.close()


def test_connection(profile: dict[str, Any]) -> dict[str, Any]:
    res = _run(profile, "echo MAGESTIC_OK && uname -a", timeout=10)
    return {
        "ok": res.exit_code == 0 and "MAGESTIC_OK" in res.stdout,
        "stdout": res.stdout.strip()[:1000],
        "stderr": res.stderr.strip()[:500],
        "exit_code": res.exit_code,
    }


def tail_log(profile: dict[str, Any], log_name: str, *, lines: int = 200) -> SshResult:
    logs = profile.get("logs") or {}
    path = logs.get(log_name)
    if not path:
        raise SshError(
            f"log {log_name!r} not in profile allowlist; add it to profile.logs[{log_name!r}] = '/abs/path'"
        )
    if not isinstance(path, str) or not path.startswith("/"):
        raise SshError("log path must be absolute")
    if lines <= 0 or lines > 5000:
        raise SshError("lines must be between 1 and 5000")
    return _run(profile, f"tail -n {int(lines)} {shlex.quote(path)}", timeout=20)


def run_deploy(profile: dict[str, Any], deploy_name: str) -> SshResult:
    deploys = profile.get("deploys") or {}
    script = deploys.get(deploy_name)
    if not script:
        raise SshError(
            f"deploy {deploy_name!r} not in profile allowlist; add it to profile.deploys[{deploy_name!r}] = '/abs/path/to/script.sh'"
        )
    if not isinstance(script, str) or not script.startswith("/"):
        raise SshError("deploy script path must be absolute")
    return _run(profile, shlex.quote(script), timeout=600)


# Args are restricted to flags (--word) and "safe" values. Everything is also
# shlex.quoted before hitting the shell, so this is defense-in-depth: it keeps
# the allowlisted-script model honest (no operators, no substitution, no paths
# with shell metacharacters sneaking through).
import re as _re  # noqa: E402

_FLAG_RE = _re.compile(r"^--[a-z][a-z0-9-]*$")
# letters, digits, and a small set of path/value-safe punctuation
_VALUE_RE = _re.compile(r"^[A-Za-z0-9_./:@=+,-]+$")


def run_script(
    profile: dict[str, Any],
    deploy_name: str,
    args: list[str] | None = None,
    *,
    timeout: int = 1800,
) -> SshResult:
    """Run an allowlisted deploy script WITH validated arguments.

    Like run_deploy(), but appends a vetted argument vector. Each arg must be
    either a ``--flag`` or a value matching a conservative safe charset; both are
    then shlex.quoted. There is still no free-form command surface — the script
    path comes only from profile.deploys[deploy_name].
    """
    deploys = profile.get("deploys") or {}
    script = deploys.get(deploy_name)
    if not script:
        raise SshError(
            f"deploy {deploy_name!r} not in profile allowlist; add it to "
            f"profile.deploys[{deploy_name!r}] = '/abs/path/to/script.sh'"
        )
    if not isinstance(script, str) or not script.startswith("/"):
        raise SshError("deploy script path must be absolute")

    parts = [shlex.quote(script)]
    for a in args or []:
        if not isinstance(a, str) or not a:
            raise SshError(f"invalid script argument: {a!r}")
        if a.startswith("--"):
            if not _FLAG_RE.match(a):
                raise SshError(f"invalid flag argument: {a!r}")
        elif not _VALUE_RE.match(a):
            raise SshError(f"argument contains unsafe characters: {a!r}")
        parts.append(shlex.quote(a))

    return _run(profile, " ".join(parts), timeout=timeout)
