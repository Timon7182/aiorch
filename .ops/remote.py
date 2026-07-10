"""Paramiko-backed remote runner for deploying MagesticAI on the LAN server.

Usage as a module:
    from remote import Remote
    r = Remote.from_env()
    r.run("docker ps")
    r.put_text("/tmp/foo.txt", "hello")

Connection settings come from env vars (SSH_HOST, SSH_USER, SSH_PASSWORD).
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass

import paramiko


@dataclass
class Result:
    stdout: str
    stderr: str
    exit_code: int


class Remote:
    def __init__(
        self,
        host: str,
        user: str,
        password: str | None = None,
        key_path: str | None = None,
    ) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {
            "hostname": host,
            "username": user,
            "timeout": 20,
        }
        if key_path:
            connect_kwargs["key_filename"] = key_path
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        elif password:
            connect_kwargs["password"] = password
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        else:
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True
        self._client.connect(**connect_kwargs)
        self._sftp: paramiko.SFTPClient | None = None

    @classmethod
    def from_env(cls) -> "Remote":
        host = os.environ.get("SSH_HOST", "192.168.88.55")
        user = os.environ.get("SSH_USER", "saya")
        key_path = os.environ.get("SSH_KEY_PATH")
        pwd = os.environ.get("SSH_PASSWORD")
        if not key_path and not pwd:
            raise RuntimeError("Set SSH_KEY_PATH or SSH_PASSWORD")
        return cls(host, user, password=pwd, key_path=key_path)

    @property
    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def run(self, cmd: str, *, timeout: int = 60, check: bool = False) -> Result:
        _, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if check and exit_code != 0:
            raise RuntimeError(
                f"remote command failed (exit {exit_code}): {cmd!r}\nSTDERR: {err}"
            )
        return Result(out, err, exit_code)

    def put_text(self, remote_path: str, content: str, *, mode: int = 0o644) -> None:
        parent = os.path.dirname(remote_path)
        if parent:
            self.run(f"mkdir -p {parent}")
        with self.sftp.file(remote_path, "w") as f:
            f.write(content)
        self.sftp.chmod(remote_path, mode)

    def echo(self, label: str, result: Result, *, max_lines: int = 30) -> None:
        out = result.stdout.strip()
        err = result.stderr.strip()
        print(f"--- {label} (exit={result.exit_code}) ---")
        if out:
            lines = out.splitlines()
            print("\n".join(lines[:max_lines]))
            if len(lines) > max_lines:
                print(f"... ({len(lines) - max_lines} more lines)")
        if err:
            print(f"STDERR: {err[:500]}")

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        self._client.close()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: remote.py <command> [args...]", file=sys.stderr)
        return 2
    r = Remote.from_env()
    try:
        cmd = " ".join(sys.argv[1:])
        res = r.run(cmd, timeout=120)
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        return res.exit_code
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
