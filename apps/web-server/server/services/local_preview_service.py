"""Local preview strategies: dev-server (hot-reload) and compose-local.

Complements the remote docker preview (``preview_deploy_service``). Instead of
building the task's worktree into a Docker stack on a remote host over SSH, these
strategies run the preview **on the machine hosting the web-server**:

  - ``dev-server``   — spawn the framework's dev server (``npm run dev`` /
                       ``manage.py runserver`` / …) as a subprocess with hot
                       reload, on an auto-allocated local port.
  - ``compose-local`` — bring the worktree up with a local ``docker compose``
                       project.

Design notes:
  - Everything runs on the web-server's asyncio loop. ``start`` is a thin sync
    entrypoint (called from the async route while the loop is running); it
    resolves the worktree, allocates a port, writes the initial "building"
    state, schedules the lifecycle coroutine, and returns immediately. The UI
    then receives ``preview:status`` / ``preview:log`` websocket events and also
    polls ``GET /deploy-preview`` as a fallback.
  - Preview state is persisted into ``<spec>/task_metadata.json`` under the same
    ``"preview"`` key the remote path uses (plus a ``"strategy"`` field) so GET
    keeps working across restarts. On restart the in-memory registry is empty,
    so ``reconcile`` marks a previously-running local preview ``stopped`` (its
    process is gone).
  - Windows + Linux: ``npm`` is resolved to ``npm.cmd`` via ``shutil.which``;
    process trees are killed with ``taskkill /T /F`` on Windows and
    ``os.killpg`` on POSIX (children spawned in a new session/process group).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..websockets import events as ws_events
from . import preview_deploy_service as pds

# Port range for auto-allocated dev servers (override via env).
_PORT_MIN = int(os.environ.get("LOCAL_PREVIEW_PORT_MIN", "14000"))
_PORT_MAX = int(os.environ.get("LOCAL_PREVIEW_PORT_MAX", "14999"))

# How long to wait for the dev server to start accepting TCP connections.
_READY_TIMEOUT_S = 120
_LOG_RING = 500


@dataclass
class LocalPreview:
    task_id: str
    project_id: str
    strategy: str
    status: str = "building"          # building | running | failed | stopped
    url: str | None = None
    port: int | None = None
    error: str | None = None
    started_at: int = field(default_factory=lambda: int(time.time()))
    spec_dir: Path | None = None
    # dev-server: the asyncio subprocess; compose-local: the compose project name
    process: "asyncio.subprocess.Process | None" = None
    compose_project: str | None = None
    compose_file: str | None = None
    cwd: Path | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_RING))
    _ready: "asyncio.Event | None" = None
    # Held task references so stop/teardown can cancel them explicitly.
    _pump_task: "asyncio.Task | None" = None
    _emit_tasks: "set[asyncio.Task]" = field(default_factory=set)


# task_id -> LocalPreview
_previews: dict[str, LocalPreview] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _running_loop() -> asyncio.AbstractEventLoop:
    """The loop to run preview work on (the web-server's main loop)."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        loop = ws_events._main_loop  # captured at app startup
        if loop is None:
            raise RuntimeError("no event loop available for local preview")
        return loop


def _resolve_exe(name: str) -> str:
    """Resolve an executable, honoring Windows' .cmd shims (npm -> npm.cmd)."""
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        found = shutil.which(f"{name}.cmd")
        if found:
            return found
        return f"{name}.cmd"
    return name


def _alloc_port(preferred: int | None = None) -> int:
    """Return a free localhost port. Prefers `preferred`, else scans the range."""
    def _free(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return True
            except OSError:
                return False

    if preferred and _free(preferred):
        return preferred
    for p in range(_PORT_MIN, _PORT_MAX + 1):
        if _free(p):
            return p
    # Fall back to an ephemeral port if the whole range is taken.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def detect_dev_command(path: Path) -> tuple[list[str], int | None]:
    """Best-effort auto-detect of a dev-server command for a project directory.

    Returns ``(argv, default_port)``. ``argv`` is empty if nothing was detected.
    Mirrors the stack heuristics in the CLI's WorktreeManager.get_test_commands
    but resolves *dev* (hot-reload) commands instead of test commands.
    """
    pkg = path / "package.json"
    if pkg.exists():
        npm = _resolve_exe("npm")
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        scripts = data.get("scripts") or {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        # Port hints from the framework in play.
        default_port: int | None = None
        if "vite" in deps:
            default_port = 5173
        elif "next" in deps:
            default_port = 3000
        elif "react-scripts" in deps:
            default_port = 3000
        elif "@angular/core" in deps:
            default_port = 4200
        script = "dev" if "dev" in scripts else ("start" if "start" in scripts else None)
        if script:
            return ([npm, "run", script], default_port)
        # No dev/start script — fall through to nothing.
        return ([], default_port)

    if (path / "manage.py").exists():
        py = _resolve_exe("python3") if os.name != "nt" else _resolve_exe("python")
        return ([py, "manage.py", "runserver"], 8000)

    if (path / "Cargo.toml").exists():
        return ([_resolve_exe("cargo"), "run"], None)

    if (path / "go.mod").exists():
        return ([_resolve_exe("go"), "run", "."], None)

    return ([], None)


def _apply_port_flags(argv: list[str], port: int, path: Path) -> list[str]:
    """Append a best-effort ``--port`` flag for known npm dev runners.

    vite uses ``--port``; next uses ``-p``. For ``npm run <script>`` the flags
    have to come after ``--`` so npm forwards them to the underlying tool.
    """
    if not argv:
        return argv
    is_npm = os.path.basename(argv[0]).lower().startswith("npm")
    if not is_npm:
        return argv
    deps: dict[str, Any] = {}
    pkg = path / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        except (json.JSONDecodeError, OSError):
            deps = {}
    if "next" in deps:
        return [*argv, "--", "-p", str(port)]
    # vite / react-scripts / angular all accept --port after `--`.
    return [*argv, "--", "--port", str(port)]


def _persist(preview: LocalPreview) -> None:
    """Mirror the in-memory preview into task_metadata.json (same shape as remote)."""
    if not preview.spec_dir:
        return
    try:
        pds._write_preview_state(preview.spec_dir, {
            "status": preview.status,
            "strategy": preview.strategy,
            "url": preview.url,
            "ip": None,
            "port": preview.port,
            "error": preview.error,
            "startedAt": preview.started_at,
        })
    except Exception:
        pass


async def _emit_status(preview: LocalPreview) -> None:
    try:
        await ws_events.emit_preview_status(
            preview.task_id, preview.project_id, preview.status,
            preview.strategy, url=preview.url, error=preview.error,
        )
    except Exception:
        pass


def _log(preview: LocalPreview, line: str) -> None:
    """Append a line to the ring buffer and stream it as a preview:log event.

    All callers (``_run_dev_server``, ``_run_compose``, ``_pump``, ``_stream``)
    are coroutines already running ON the main event loop, so the emit is
    scheduled with ``create_task``. Task references are held on the preview
    record so they can be cancelled on stop.
    """
    line = line.rstrip("\n")
    preview.logs.append(line)
    try:
        task = asyncio.create_task(ws_events.emit_preview_log(preview.task_id, line))
        preview._emit_tasks.add(task)
        task.add_done_callback(preview._emit_tasks.discard)
    except Exception:
        pass


def _state_dict(preview: LocalPreview) -> dict[str, Any]:
    return {
        "status": preview.status,
        "strategy": preview.strategy,
        "url": preview.url,
        "ip": None,
        "port": preview.port,
        "error": preview.error,
        "startedAt": preview.started_at,
        "updatedAt": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def start(ref: pds.TaskRef, strategy: str, config: dict[str, Any]) -> dict[str, Any]:
    """Begin a local preview and return the initial "building" state.

    Schedules the async lifecycle on the running loop; the UI gets live updates
    over websockets and can poll GET as a fallback.
    """
    # Tear down any prior local preview for this task first.
    task_id = f"{ref.project_id}:{ref.spec_id}"
    existing = _previews.get(task_id)
    if existing:
        _kill(existing)

    branch = pds._branch_of(ref.worktree_path)
    sha = pds._short_sha(ref.worktree_path)

    preview = LocalPreview(
        task_id=f"{ref.project_id}:{ref.spec_id}",
        project_id=ref.project_id,
        strategy=strategy,
        spec_dir=ref.spec_dir,
    )
    preview._ready = asyncio.Event()
    _previews[preview.task_id] = preview

    initial_state: dict[str, Any] = {
        "status": "building",
        "strategy": strategy,
        "branch": branch,
        "ref": sha,
        "url": None,
        "ip": None,
        "port": None,
        "error": None,
        "startedAt": preview.started_at,
    }
    if strategy == "compose-local":
        # Persist the compose file so teardown works after a server restart
        # (when the in-memory handle is gone).
        initial_state["composeFile"] = config.get("composeFile") or "docker-compose.yml"
    pds._write_preview_state(ref.spec_dir, initial_state)

    loop = _running_loop()
    if strategy == "compose-local":
        loop.create_task(_run_compose(preview, ref, config))
    else:
        loop.create_task(_run_dev_server(preview, ref, config))

    return {**_state_dict(preview), "branch": branch, "ref": sha}


async def _run_dev_server(preview: LocalPreview, ref: pds.TaskRef, config: dict[str, Any]) -> None:
    try:
        ds = config.get("devServer") or {}
        # Working directory: worktree root, optionally a component subdir.
        cwd = ref.worktree_path
        if ds.get("cwd"):
            cwd = (ref.worktree_path / ds["cwd"]).resolve()
        preview.cwd = cwd

        # Command: explicit config, else auto-detect.
        default_port: int | None = None
        if ds.get("command"):
            cmd = ds["command"]
            argv = cmd if isinstance(cmd, list) else _split_command(cmd)
        else:
            argv, default_port = detect_dev_command(cwd)
        if not argv:
            raise RuntimeError(
                "could not detect a dev-server command for this project; "
                "set strategy=dev-server + devServer.command in deploy.config.json"
            )

        port = _alloc_port(ds.get("port") or default_port)
        preview.port = port
        argv = _apply_port_flags(argv, port, cwd)

        env = os.environ.copy()
        env["PORT"] = str(port)
        env["BROWSER"] = "none"  # keep CRA/react-scripts from opening a browser
        env.update({str(k): str(v) for k, v in (ds.get("env") or {}).items()})

        ready_pattern = ds.get("readyPattern")

        _log(preview, f"$ {' '.join(argv)}  (cwd={cwd}, PORT={port})")

        # New session/process group so we can kill the whole tree later.
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except FileNotFoundError as exc:
            raise RuntimeError(f"command not found: {argv[0]} ({exc})") from exc
        preview.process = proc

        preview._pump_task = asyncio.create_task(_pump(preview, ready_pattern))

        # Readiness: TCP connect, log pattern, or early exit.
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                tail = "\n".join(list(preview.logs)[-15:])
                raise RuntimeError(f"dev server exited ({proc.returncode}) before ready:\n{tail}")
            if preview._ready and preview._ready.is_set():
                break
            if await _tcp_ok(port):
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f"dev server did not become ready within {_READY_TIMEOUT_S}s")

        preview.status = "running"
        preview.url = f"http://localhost:{port}"
        preview.error = None
        _persist(preview)
        await _emit_status(preview)
        _log(preview, f"ready at {preview.url}")
    except Exception as exc:  # noqa: BLE001 — surface failure to the UI
        preview.status = "failed"
        preview.error = str(exc)
        _persist(preview)
        await _emit_status(preview)
        _kill(preview)


async def _run_compose(preview: LocalPreview, ref: pds.TaskRef, config: dict[str, Any]) -> None:
    try:
        cwd = ref.worktree_path
        preview.cwd = cwd
        compose_file = config.get("composeFile") or "docker-compose.yml"
        preview.compose_file = compose_file
        if not (cwd / compose_file).exists():
            raise RuntimeError(f"{compose_file} not found in worktree")

        project = f"magestic-preview-{ref.slug}"[:60]
        preview.compose_project = project

        up = ["docker", "compose", "-f", compose_file, "-p", project, "up", "-d", "--build"]
        _log(preview, f"$ {' '.join(up)}")
        code = await _stream(preview, up, cwd)
        if code != 0:
            tail = "\n".join(list(preview.logs)[-15:])
            raise RuntimeError(f"docker compose up failed ({code}):\n{tail}")

        # Resolve the published URL: configured port, else `compose port`.
        port = config.get("port")
        service = config.get("service")
        if not port and service:
            container_port = config.get("containerPort") or 80
            port = await _compose_published_port(
                cwd, compose_file, project, service, container_port,
            )
        preview.port = port
        preview.url = f"http://localhost:{port}" if port else None

        preview.status = "running"
        preview.error = None
        _persist(preview)
        await _emit_status(preview)
        _log(preview, f"compose project {project} up" + (f" at {preview.url}" if preview.url else ""))
    except Exception as exc:  # noqa: BLE001
        preview.status = "failed"
        preview.error = str(exc)
        _persist(preview)
        await _emit_status(preview)


async def _pump(preview: LocalPreview, ready_pattern: str | None) -> None:
    """Read merged stdout/stderr line-by-line into the ring buffer + WS logs."""
    proc = preview.process
    if not proc or not proc.stdout:
        return
    while True:
        try:
            raw = await proc.stdout.readline()
        except Exception:
            break
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        _log(preview, line)
        if ready_pattern and preview._ready and not preview._ready.is_set():
            try:
                if re.search(ready_pattern, line):
                    preview._ready.set()
            except re.error:
                if ready_pattern in line:
                    preview._ready.set()


async def _tcp_ok(port: int) -> bool:
    try:
        fut = asyncio.open_connection("127.0.0.1", port)
        reader, writer = await asyncio.wait_for(fut, timeout=1.0)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _stream(preview: LocalPreview, argv: list[str], cwd: Path) -> int:
    """Run a subprocess to completion, streaming merged output into the buffer."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        _log(preview, f"command not found: {argv[0]} ({exc})")
        return 127
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        _log(preview, raw.decode("utf-8", "replace").rstrip("\r\n"))
    return await proc.wait()


async def _compose_published_port(
    cwd: Path, compose_file: str, project: str, service: str, container_port: int = 80,
) -> int | None:
    """Ask `docker compose port` for the host port published for a service's
    container port (``containerPort`` in the compose-local config, default 80)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", compose_file, "-p", project,
            "port", service, str(container_port),
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", "replace").strip()
        # Format: 0.0.0.0:49153
        if ":" in text:
            return int(text.rsplit(":", 1)[1])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Stop / reconcile / shutdown
# ---------------------------------------------------------------------------
def _kill(preview: LocalPreview) -> None:
    """Kill the dev-server process tree, or tear down the compose project."""
    # Cancel the output pump and any in-flight log-emit tasks first so nothing
    # keeps reading from (or emitting for) a process we're about to kill.
    if preview._pump_task and not preview._pump_task.done():
        preview._pump_task.cancel()
    for task in list(preview._emit_tasks):
        if not task.done():
            task.cancel()
    preview._emit_tasks.clear()
    if preview.process and preview.process.returncode is None:
        pid = preview.process.pid
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True,
                )
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        except Exception:
            pass
    if preview.compose_project and preview.cwd and preview.compose_file:
        try:
            subprocess.run(
                ["docker", "compose", "-f", preview.compose_file,
                 "-p", preview.compose_project, "down", "-v", "--remove-orphans"],
                cwd=str(preview.cwd), capture_output=True, timeout=120,
            )
        except Exception:
            pass


def stop(ref: pds.TaskRef) -> dict[str, Any]:
    """Stop a local preview (kill process / compose down) and persist stopped."""
    task_id = f"{ref.project_id}:{ref.spec_id}"
    preview = _previews.get(task_id)
    if preview:
        _kill(preview)
        preview.status = "stopped"
        preview.url = None
        _persist(preview)
        try:
            loop = ws_events._main_loop
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(_emit_status(preview), loop)
        except Exception:
            pass
        _previews.pop(task_id, None)
    else:
        # No in-memory handle (e.g. after a restart). Best-effort compose down
        # using the persisted marker, then mark stopped.
        _compose_down_from_meta(ref)

    return pds._write_preview_state(ref.spec_dir, {
        "status": "stopped", "url": None, "ip": None, "port": None,
    })


def _compose_down_from_meta(ref: pds.TaskRef) -> None:
    """Tear down a compose-local preview using only persisted state.

    Used when there's no in-memory handle (server restarted). The compose file
    is read from the preview state persisted at start so previews launched with
    a custom ``composeFile`` are torn down correctly too.
    """
    meta_preview = (pds._read_meta(ref.spec_dir).get("preview")) or {}
    if meta_preview.get("strategy") != "compose-local":
        return
    project = f"magestic-preview-{ref.slug}"[:60]
    compose_file = meta_preview.get("composeFile") or "docker-compose.yml"
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_file, "-p", project,
             "down", "-v", "--remove-orphans"],
            cwd=str(ref.worktree_path), capture_output=True, timeout=120,
        )
    except Exception:
        pass


def reconcile(ref: pds.TaskRef, preview_state: dict[str, Any]) -> dict[str, Any]:
    """Return an up-to-date state dict for a local preview.

    If a live in-memory handle exists, reflect it. Otherwise (server restarted
    or process gone) a persisted transient/running state is stale — mark it
    stopped so the UI doesn't show a dead preview as running.
    """
    task_id = f"{ref.project_id}:{ref.spec_id}"
    preview = _previews.get(task_id)
    if preview:
        merged = dict(preview_state)
        merged.update(_state_dict(preview))
        return merged

    status = preview_state.get("status")
    if status in ("building", "running", "deploying", "promoting"):
        return pds._write_preview_state(ref.spec_dir, {
            "status": "stopped", "url": None, "ip": None, "port": None,
        })
    return preview_state


def shutdown_all() -> None:
    """Kill all live local previews (called from the FastAPI lifespan shutdown)."""
    for preview in list(_previews.values()):
        _kill(preview)
        preview.status = "stopped"
        _persist(preview)
    _previews.clear()


def _split_command(cmd: str) -> list[str]:
    import shlex
    return shlex.split(cmd, posix=(os.name != "nt"))
