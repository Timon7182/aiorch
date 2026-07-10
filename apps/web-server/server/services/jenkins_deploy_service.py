"""Jenkins deploy orchestrator ("Развернуть на Jenkins").

For projects whose deploys go through a parameterized Jenkins job (e.g. kms →
kms.build.deploy.war building a Bitbucket branch), this service turns a finished
task into a running deploy:

  1. (optional library step) if the task changed the configured library repo
     (e.g. talentsuite), bump its version counter, run its gradle publish tasks
     (uploadArchives → Nexus), and point the consumer repo's dependency at the
     new version — committing both bumps onto the task branches.
  2. push the task branch(es) to the git remote (Jenkins builds from Bitbucket).
  3. trigger the Jenkins job with the branch parameter and follow the queue item
     → build → result, streaming progress to the UI.

Per-project config lives in deploy.config.json under a top-level "jenkins" key
(see JENKINS_DEFAULTS). Credentials + tool paths come from the project's
.magestic-ai/.env (JENKINS_URL / JENKINS_USER / JENKINS_API_TOKEN, plus
JENKINS_DEPLOY_JAVA_HOME / JENKINS_DEPLOY_GRADLE_USER_HOME for the gradle step),
falling back to the process environment.

State is persisted per task in <spec_dir>/task_metadata.json under a "jenkins"
key (mirrors the "preview" key) and streamed over the jenkins:status /
jenkins:log WebSocket events. The long-running work happens on a background
thread; the UI polls GET /tasks/{id}/deploy-jenkins.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import deploy_config as dc
from . import preview_deploy_service as pds

logger = logging.getLogger(__name__)


class JenkinsError(RuntimeError):
    pass


JENKINS_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Job path relative to the Jenkins root URL, e.g. "job/kms.build.deploy.war".
    "job": "",
    # Name of the string parameter that receives the task branch.
    "branchParam": "BRANCH_NAME",
    # Extra fixed build parameters ({"DEPLOYMENT_SERVER": "dev.uco.kz"}).
    "params": {},
    # Multi-repo: child repo whose branch is what Jenkins builds. Omit/None for
    # single-repo projects (the task worktree itself).
    "deployRepo": None,
    # How long to wait for the Jenkins build to finish before giving up (the
    # build keeps running on Jenkins; we just stop following it).
    "buildTimeoutMinutes": 45,
    # Optional library publish step — see module docstring. None disables it.
    "library": None,
}

LIBRARY_DEFAULTS: dict[str, Any] = {
    "repo": "",                     # child repo name, e.g. "talentsuite"
    "versionFile": "build.gradle",
    # Version line pattern; {n} marks the numeric counter to bump.
    "versionPattern": "version = \"{n}\"",
    "gradleTasks": ["uploadArchives"],
    "gradleArgs": [],
    "gradleTimeoutMinutes": 40,
    # Consumer dependency to repoint at the new version.
    "consumerRepo": "",             # e.g. "kms" (defaults to deployRepo)
    "consumerFile": "build.gradle",
    "consumerPattern": "",          # e.g. "kz.uco.tsadv:tsadv-global:kms.1.0.0.{n}-SNAPSHOT"
}

# Statuses the UI treats as "in flight" (it keeps polling while one is active).
TRANSIENT_STATUSES = ("bumping", "publishing", "pushing", "triggering", "queued", "building")


# ---------------------------------------------------------------------------
# Config + environment
# ---------------------------------------------------------------------------
def load_jenkins_config(project_path: Path) -> dict[str, Any] | None:
    """The project's merged "jenkins" section, or None when not configured."""
    path = dc.config_file(project_path)
    if not path:
        return None
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    section = raw.get("jenkins") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return None
    cfg = {**JENKINS_DEFAULTS, **section}
    if isinstance(section.get("library"), dict):
        cfg["library"] = {**LIBRARY_DEFAULTS, **section["library"]}
    else:
        cfg["library"] = None
    return cfg if cfg.get("enabled") else None


def _load_env_file(path: Path, into: dict[str, str]) -> None:
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            into.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def _project_env(project_path: Path) -> dict[str, str]:
    """Project .magestic-ai/.env values with process-env fallback."""
    env: dict[str, str] = {}
    _load_env_file(project_path / ".magestic-ai" / ".env", env)
    for key in (
        "JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN",
        "JENKINS_DEPLOY_JAVA_HOME", "JENKINS_DEPLOY_GRADLE_USER_HOME",
    ):
        if key not in env and os.environ.get(key):
            env[key] = os.environ[key]
    return env


# ---------------------------------------------------------------------------
# Per-task state (task_metadata.json "jenkins" key)
# ---------------------------------------------------------------------------
def _write_state(spec_dir: Path, patch: dict[str, Any]) -> dict[str, Any]:
    f = spec_dir / "task_metadata.json"
    meta: dict[str, Any] = {}
    if f.exists():
        try:
            meta = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            meta = {}
    state = dict(meta.get("jenkins") or {})
    state.update(patch)
    state["updatedAt"] = int(time.time())
    meta["jenkins"] = state
    f.write_text(json.dumps(meta, indent=2))
    return state


def get_state(task_id: str) -> dict[str, Any]:
    """Current deploy state + whether the project has Jenkins configured at all
    (the UI hides the panel when enabled=False)."""
    ref = pds.resolve_task(task_id)
    cfg = load_jenkins_config(ref.project_path)
    meta_file = ref.spec_dir / "task_metadata.json"
    state: dict[str, Any] = {"status": "none"}
    if meta_file.exists():
        try:
            state = json.loads(meta_file.read_text()).get("jenkins") or {"status": "none"}
        except (json.JSONDecodeError, OSError):
            pass
    state["enabled"] = cfg is not None
    return state


# ---------------------------------------------------------------------------
# Git helpers (worktree-level)
# ---------------------------------------------------------------------------
def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def _git_ok(args: list[str], cwd: Path, timeout: int = 60) -> str:
    r = _git(args, cwd, timeout)
    if r.returncode != 0:
        raise JenkinsError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()


def _worktrees_for(ref: pds.TaskRef) -> list[dict[str, Any]]:
    """[{name, path, changed}] — per-repo for composite tasks, single entry otherwise."""
    composite = pds._composite_worktrees(ref.worktree_path)
    if composite:
        return composite
    if (ref.worktree_path / ".git").exists():
        return [{"name": ref.worktree_path.name, "path": ref.worktree_path, "changed": True}]
    return []


def _base_branch_of(worktree: Path) -> str | None:
    """The branch checked out in the repo's main worktree (= merge target)."""
    out = _git(["worktree", "list", "--porcelain"], worktree)
    if out.returncode != 0:
        return None
    for ln in out.stdout.split("\n\n")[0].splitlines():
        if ln.startswith("branch "):
            return ln.split(" ", 1)[1].strip().replace("refs/heads/", "")
    return None


def _fork_point(worktree: Path) -> str | None:
    """The commit the task branch was created at (oldest reflog entry).

    More reliable than diffing against the repo's checked-out base branch:
    when base auto-detection cut the worktree from a different branch than the
    main checkout (e.g. talentsuite worktree from ``master`` while the checkout
    sits on ``kms-master``), a base-branch diff reports the whole divergence as
    "task changes". The branch's creation point never lies about that.
    """
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree)
    if branch.returncode != 0 or not branch.stdout.strip():
        return None
    out = _git(["log", "-g", "--format=%H", branch.stdout.strip()], worktree)
    if out.returncode != 0:
        return None
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _task_changed_repo(worktree: Path, fallback: bool) -> bool:
    """Did the task itself change this repo? Commits since the branch's fork
    point, or dirty tracked files. Falls back to the caller's flag when the
    reflog is unavailable."""
    dirty = _git(["status", "--porcelain", "--untracked-files=no"], worktree)
    if dirty.returncode == 0 and dirty.stdout.strip():
        return True
    fork = _fork_point(worktree)
    if not fork:
        return fallback
    cnt = _git(["rev-list", "--count", f"{fork}..HEAD"], worktree)
    if cnt.returncode != 0:
        return fallback
    return cnt.stdout.strip() != "0"


# ---------------------------------------------------------------------------
# Version bump helpers
# ---------------------------------------------------------------------------
def _pattern_to_regex(pattern: str) -> re.Pattern:
    """Turn a "literal text {n} literal text" pattern into a capturing regex."""
    if "{n}" not in pattern:
        raise JenkinsError(f"pattern must contain {{n}} placeholder: {pattern!r}")
    head, _, tail = pattern.partition("{n}")
    return re.compile(re.escape(head) + r"(\d+)" + re.escape(tail))


def _current_counter(text: str, pattern: str) -> int | None:
    m = _pattern_to_regex(pattern).search(text)
    return int(m.group(1)) if m else None


def _replace_counter(text: str, pattern: str, value: int) -> tuple[str, bool]:
    rx = _pattern_to_regex(pattern)
    head, _, tail = pattern.partition("{n}")
    new_text, count = rx.subn(head + str(value) + tail, text, count=1)
    return new_text, count > 0


# ---------------------------------------------------------------------------
# Jenkins REST helpers
# ---------------------------------------------------------------------------
def _jenkins_base(cfg: dict[str, Any], env: dict[str, str]) -> tuple[str, tuple[str, str]]:
    url = (cfg.get("url") or env.get("JENKINS_URL") or "").rstrip("/")
    user = env.get("JENKINS_USER") or ""
    token = env.get("JENKINS_API_TOKEN") or ""
    if not url:
        raise JenkinsError("JENKINS_URL is not configured (project .magestic-ai/.env)")
    if not user or not token:
        raise JenkinsError("JENKINS_USER / JENKINS_API_TOKEN are not configured (project .magestic-ai/.env)")
    return url, (user, token)


def _trigger_build(cfg: dict[str, Any], env: dict[str, str], branch: str) -> str:
    """POST buildWithParameters; returns the queue item URL."""
    import httpx

    base, auth = _jenkins_base(cfg, env)
    job = (cfg.get("job") or "").strip("/")
    if not job:
        raise JenkinsError("jenkins.job is not configured (deploy.config.json)")
    params = {cfg.get("branchParam") or "BRANCH_NAME": branch}
    params.update(cfg.get("params") or {})
    resp = httpx.post(
        f"{base}/{job}/buildWithParameters",
        data=params, auth=auth, timeout=30, follow_redirects=False,
    )
    if resp.status_code not in (200, 201, 302):
        raise JenkinsError(f"Jenkins trigger failed: HTTP {resp.status_code} {resp.text[:300]}")
    location = resp.headers.get("location") or ""
    if not location:
        raise JenkinsError("Jenkins accepted the build but returned no queue URL")
    return location.rstrip("/")


def _follow_build(
    cfg: dict[str, Any],
    env: dict[str, str],
    queue_url: str,
    on_update,
) -> dict[str, Any]:
    """Poll queue item → build → result. Returns {url, number, result}."""
    import httpx

    _, auth = _jenkins_base(cfg, env)
    deadline = time.time() + int(cfg.get("buildTimeoutMinutes") or 45) * 60
    build_url: str | None = None
    build_number: int | None = None

    with httpx.Client(auth=auth, timeout=30) as client:
        # Queue phase: wait for the item to leave the queue (become a build).
        while build_url is None:
            if time.time() > deadline:
                raise JenkinsError("timed out waiting for Jenkins to start the build")
            r = client.get(f"{queue_url}/api/json")
            if r.status_code == 404:
                # Jenkins reaps finished queue items after ~5 min; without the
                # executable pointer we can't locate the build reliably.
                raise JenkinsError("Jenkins queue item disappeared before the build started")
            r.raise_for_status()
            data = r.json()
            if data.get("cancelled"):
                raise JenkinsError("Jenkins build was cancelled while queued")
            executable = data.get("executable") or {}
            if executable.get("url"):
                build_url = str(executable["url"]).rstrip("/")
                build_number = executable.get("number")
                on_update("building", build_url=build_url, build_number=build_number)
                break
            time.sleep(3)

        # Build phase: wait for a result.
        while True:
            if time.time() > deadline:
                raise JenkinsError(
                    f"timed out waiting for the Jenkins build to finish: {build_url}"
                )
            r = client.get(f"{build_url}/api/json", params={"tree": "result,building,number,url"})
            r.raise_for_status()
            data = r.json()
            if not data.get("building") and data.get("result"):
                return {"url": build_url, "number": data.get("number", build_number), "result": data["result"]}
            time.sleep(5)


# ---------------------------------------------------------------------------
# Library publish step (version bump + gradle uploadArchives + consumer bump)
# ---------------------------------------------------------------------------
def _run_gradle(
    lib: dict[str, Any],
    lib_wt: Path,
    env: dict[str, str],
    on_line,
) -> None:
    java_home = env.get("JENKINS_DEPLOY_JAVA_HOME") or os.environ.get("JAVA_HOME") or ""
    gradle_home = env.get("JENKINS_DEPLOY_GRADLE_USER_HOME") or ""
    run_env = dict(os.environ)
    if java_home:
        run_env["JAVA_HOME"] = java_home
        run_env["PATH"] = f"{java_home}/bin:" + run_env.get("PATH", "")
    if gradle_home:
        run_env["GRADLE_USER_HOME"] = gradle_home

    gradlew = lib_wt / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if not gradlew.exists():
        raise JenkinsError(f"no gradle wrapper in {lib_wt}")
    if os.name != "nt":
        try:
            gradlew.chmod(gradlew.stat().st_mode | 0o111)
        except OSError:
            pass

    cmd = [str(gradlew), *list(lib.get("gradleArgs") or []), *list(lib.get("gradleTasks") or [])]
    on_line("$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, cwd=str(lib_wt), env=run_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    tail: list[str] = []
    deadline = time.time() + int(lib.get("gradleTimeoutMinutes") or 40) * 60
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        tail.append(line)
        if len(tail) > 80:
            tail.pop(0)
        on_line(line)
        if time.time() > deadline:
            proc.kill()
            raise JenkinsError("gradle publish timed out")
    code = proc.wait()
    if code != 0:
        raise JenkinsError("gradle publish failed:\n" + "\n".join(tail[-25:]))


def _publish_library(
    cfg: dict[str, Any],
    env: dict[str, str],
    worktrees: list[dict[str, Any]],
    deploy_wt: Path,
    on_update,
    on_line,
) -> int | None:
    """Run the library bump→publish→consumer-bump chain. Returns the published
    version counter, or None when the library repo didn't change (step skipped)."""
    lib = cfg.get("library") or None
    if not lib or not lib.get("repo"):
        return None
    lib_entry = next((w for w in worktrees if w["name"] == lib["repo"]), None)
    if lib_entry is None or not _task_changed_repo(
        lib_entry["path"], bool(lib_entry.get("changed"))
    ):
        on_line(f"[library] {lib.get('repo')}: no changes — skipping publish")
        return None
    lib_wt: Path = lib_entry["path"]

    on_update("bumping")
    version_file = lib_wt / lib["versionFile"]
    try:
        text = version_file.read_text()
    except OSError as exc:
        raise JenkinsError(f"cannot read {version_file}: {exc}") from exc
    pattern = lib["versionPattern"]
    current = _current_counter(text, pattern)
    if current is None:
        raise JenkinsError(f"version pattern {pattern!r} not found in {version_file}")

    # Baseline = the version on the repo's base branch. Already-bumped branches
    # (a redeploy, or the agent bumped it itself) keep their counter; otherwise
    # bump one past the base so parallel work on the base version is untouched.
    base_branch = _base_branch_of(lib_wt)
    base = None
    if base_branch:
        show = _git(["show", f"{base_branch}:{lib['versionFile']}"], lib_wt)
        if show.returncode == 0:
            base = _current_counter(show.stdout, pattern)
    target = current if (base is None or current > base) else base + 1

    if target != current:
        new_text, replaced = _replace_counter(text, pattern, target)
        if not replaced:
            raise JenkinsError(f"failed to apply version bump in {version_file}")
        version_file.write_text(new_text)
        on_line(f"[library] version {current} -> {target} in {lib['versionFile']}")
        _git_ok(["add", lib["versionFile"]], lib_wt)
        _git_ok(["commit", "-m", f"chore: bump {lib['repo']} version to {target} for deploy"], lib_wt)
    else:
        on_line(f"[library] version already at {target}")

    on_update("publishing", lib_version=target)
    _run_gradle(lib, lib_wt, env, on_line)

    # Point the consumer's dependency at the published version.
    consumer_pattern = lib.get("consumerPattern") or ""
    if consumer_pattern:
        consumer_file = deploy_wt / (lib.get("consumerFile") or "build.gradle")
        try:
            ctext = consumer_file.read_text()
        except OSError as exc:
            raise JenkinsError(f"cannot read {consumer_file}: {exc}") from exc
        cur = _current_counter(ctext, consumer_pattern)
        if cur is None:
            raise JenkinsError(f"consumer pattern {consumer_pattern!r} not found in {consumer_file}")
        if cur != target:
            new_ctext, replaced = _replace_counter(ctext, consumer_pattern, target)
            if not replaced:
                raise JenkinsError(f"failed to update dependency version in {consumer_file}")
            consumer_file.write_text(new_ctext)
            on_line(f"[library] consumer dependency {cur} -> {target}")
            _git_ok(["add", lib.get("consumerFile") or "build.gradle"], deploy_wt)
            _git_ok(["commit", "-m", f"chore: bump {lib['repo']} dependency to {target}"], deploy_wt)
    return target


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------
def deploy(task_id: str) -> dict[str, Any]:
    """Validate, set state, and run the publish→push→trigger chain in the
    background. Returns the initial state immediately."""
    ref = pds.resolve_task(task_id)
    cfg = load_jenkins_config(ref.project_path)
    if not cfg:
        raise JenkinsError("this project has no jenkins section in deploy.config.json")
    if not ref.worktree_path.exists():
        raise JenkinsError("No worktree found for this task")

    env = _project_env(ref.project_path)
    _jenkins_base(cfg, env)  # fail fast on missing credentials

    worktrees = _worktrees_for(ref)
    if not worktrees:
        raise JenkinsError("No git worktree found for this task")

    deploy_repo = cfg.get("deployRepo")
    if deploy_repo:
        deploy_entry = next((w for w in worktrees if w["name"] == deploy_repo), None)
        if deploy_entry is None:
            raise JenkinsError(f"deploy repo {deploy_repo!r} not found in the task worktree")
    else:
        deploy_entry = worktrees[0]
    deploy_wt: Path = deploy_entry["path"]

    branch = _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], deploy_wt)
    state = _write_state(ref.spec_dir, {
        "status": "bumping",
        "branch": branch,
        "buildUrl": None,
        "buildNumber": None,
        "result": None,
        "libVersion": None,
        "error": None,
        "startedAt": int(time.time()),
    })

    from ..websockets import events as ws_events

    def _emit_status(status: str, **fields: Any) -> None:
        ws_events.emit_threadsafe(ws_events.emit_jenkins_status(
            task_id, ref.project_id, status,
            build_url=fields.get("build_url"), error=fields.get("error"),
        ))

    def _on_update(status: str, build_url: str | None = None,
                   build_number: int | None = None, lib_version: int | None = None) -> None:
        patch: dict[str, Any] = {"status": status}
        if build_url is not None:
            patch["buildUrl"] = build_url
        if build_number is not None:
            patch["buildNumber"] = build_number
        if lib_version is not None:
            patch["libVersion"] = lib_version
        _write_state(ref.spec_dir, patch)
        _emit_status(status, build_url=build_url)

    def _on_line(line: str) -> None:
        ws_events.emit_threadsafe(ws_events.emit_jenkins_log(task_id, line))

    def _worker() -> None:
        try:
            _publish_library(cfg, env, worktrees, deploy_wt, _on_update, _on_line)

            _on_update("pushing")
            # Push every changed repo so Bitbucket has both the library and the
            # consumer branches (the library push is informational; the deploy
            # branch push is what Jenkins builds).
            for w in worktrees:
                if w["path"] != deploy_wt and not _task_changed_repo(
                    w["path"], bool(w.get("changed"))
                ):
                    continue
                wt_branch = _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], w["path"])
                _on_line(f"[push] {w['name']}: {wt_branch}")
                _git_ok(
                    ["push", "--force-with-lease", "origin", f"HEAD:refs/heads/{wt_branch}"],
                    w["path"], timeout=180,
                )

            _on_update("triggering")
            queue_url = _trigger_build(cfg, env, branch)
            _on_update("queued")
            _on_line(f"[jenkins] queued: {queue_url}")

            done = _follow_build(cfg, env, queue_url, _on_update)
            ok = done.get("result") == "SUCCESS"
            _write_state(ref.spec_dir, {
                "status": "success" if ok else "failed",
                "buildUrl": done.get("url"),
                "buildNumber": done.get("number"),
                "result": done.get("result"),
                "error": None if ok else f"Jenkins build finished with {done.get('result')}",
            })
            _emit_status("success" if ok else "failed", build_url=done.get("url"),
                         error=None if ok else f"Jenkins build finished with {done.get('result')}")
        except Exception as exc:  # noqa: BLE001 — record failure for the UI
            logger.warning("[jenkins] deploy failed for %s", task_id, exc_info=True)
            _write_state(ref.spec_dir, {"status": "failed", "error": str(exc)})
            _emit_status("failed", error=str(exc))

    threading.Thread(target=_worker, name=f"jenkins-deploy-{ref.slug}", daemon=True).start()
    state["enabled"] = True
    return state
