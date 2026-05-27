"""Host-aware pull/merge request creation for GitHub, GitLab, and Bitbucket.

GitHub PRs are created via the ``gh`` CLI by the caller; this module covers
GitLab merge requests and Bitbucket pull requests through their REST APIs,
authenticated with the tokens forwarded into the container environment
(``GITLAB_TOKEN``, ``BITBUCKET_TOKEN``/``BITBUCKET_APP_PASSWORD``). Pushing the
branch itself is handled separately by the git credential helper configured at
the container entrypoint — here we only need the API tokens to open the request.

The single entry point is :func:`create_merge_request`. :func:`detect_provider`
classifies a git remote URL so the caller can decide between the ``gh`` path
and this module.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

import httpx

_TIMEOUT = 30.0


def detect_provider(remote_url: str) -> dict[str, str]:
    """Parse a git remote URL into ``{kind, host, path, remote}``.

    Handles both ``git@host:owner/repo.git`` and
    ``https://[user@]host/owner/repo.git`` forms. ``path`` keeps the full
    namespace (GitLab subgroups produce ``group/subgroup/repo``). ``kind`` is
    one of ``github`` / ``gitlab`` / ``bitbucket`` / ``unknown``; self-hosted
    GitLab/Bitbucket are matched against ``GITLAB_HOST`` / ``BITBUCKET_HOST``.
    """
    url = (remote_url or "").strip()
    host = path = ""

    ssh_m = re.match(r"^[A-Za-z0-9._-]+@([^:]+):(.+?)(?:\.git)?/?$", url)
    if ssh_m:
        host, path = ssh_m.group(1), ssh_m.group(2)
    else:
        https_m = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", url)
        if https_m:
            host, path = https_m.group(1), https_m.group(2)

    if not host:
        return {"kind": "unknown", "host": "", "path": "", "remote": url}

    host_l = host.lower()
    gl_host = (os.environ.get("GITLAB_HOST") or "").lower()
    bb_host = (os.environ.get("BITBUCKET_HOST") or "").lower()

    if "github" in host_l:
        kind = "github"
    elif "gitlab" in host_l or (gl_host and host_l == gl_host):
        kind = "gitlab"
    elif "bitbucket" in host_l or (bb_host and host_l == bb_host):
        kind = "bitbucket"
    else:
        kind = "unknown"

    return {"kind": kind, "host": host, "path": path, "remote": url}


def create_merge_request(
    *,
    remote_url: str,
    head: str,
    base: str,
    title: str,
    body: str = "",
    draft: bool = False,
) -> dict[str, Any]:
    """Open a merge/pull request on the host implied by ``remote_url``.

    Returns ``{success, url, number, provider}`` on success, or
    ``{success: False, error}``. Only GitLab and Bitbucket are handled here;
    GitHub is expected to go through the ``gh`` CLI in the caller.
    """
    info = detect_provider(remote_url)
    kind = info["kind"]
    if kind == "gitlab":
        return _gitlab_create_mr(info["host"], info["path"], head, base, title, body, draft)
    if kind == "bitbucket":
        return _bitbucket_create_pr(info["host"], info["path"], head, base, title, body, draft)
    return {"success": False, "error": f"Unsupported git host for remote: {remote_url}"}


def _gitlab_create_mr(
    host: str, path: str, head: str, base: str, title: str, body: str, draft: bool
) -> dict[str, Any]:
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        return {"success": False, "error": "GITLAB_TOKEN is not configured in the server environment"}

    project = urllib.parse.quote(path, safe="")
    api = f"https://{host}/api/v4/projects/{project}/merge_requests"
    payload = {
        "source_branch": head,
        "target_branch": base,
        # GitLab marks a draft by a "Draft:" title prefix.
        "title": (f"Draft: {title}" if draft else title),
        "description": body or "",
    }
    try:
        resp = httpx.post(api, headers={"PRIVATE-TOKEN": token}, json=payload, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"GitLab API request failed: {exc}"}

    if resp.status_code in (200, 201):
        data = resp.json()
        return {
            "success": True,
            "url": data.get("web_url"),
            "number": data.get("iid"),
            "provider": "gitlab",
        }
    if resp.status_code == 409:
        return {"success": False, "error": "A merge request for this branch already exists on GitLab"}
    return {"success": False, "error": f"GitLab API {resp.status_code}: {resp.text[:300]}"}


def _bitbucket_create_pr(
    host: str, path: str, head: str, base: str, title: str, body: str, draft: bool
) -> dict[str, Any]:
    token = os.environ.get("BITBUCKET_TOKEN") or os.environ.get("BITBUCKET_APP_PASSWORD")
    username = os.environ.get("BITBUCKET_USERNAME")
    if not token:
        return {"success": False, "error": "BITBUCKET_TOKEN/BITBUCKET_APP_PASSWORD is not configured"}

    is_cloud = "bitbucket.org" in host.lower()

    if is_cloud:
        # Cloud: path is "workspace/repo".
        workspace, _, repo = path.partition("/")
        repo = repo.strip("/")
        api = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/pullrequests"
        payload = {
            "title": title,
            "description": body or "",
            "source": {"branch": {"name": head}},
            "destination": {"branch": {"name": base}},
        }
        # Cloud auth: USERNAME + app password (basic) or a bare access token (bearer).
        auth = (username, token) if username else None
        headers = {} if username else {"Authorization": f"Bearer {token}"}
        try:
            resp = httpx.post(api, json=payload, auth=auth, headers=headers, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            return {"success": False, "error": f"Bitbucket API request failed: {exc}"}
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "success": True,
                "url": (data.get("links") or {}).get("html", {}).get("href"),
                "number": data.get("id"),
                "provider": "bitbucket",
            }
        return {"success": False, "error": f"Bitbucket API {resp.status_code}: {resp.text[:300]}"}

    # Server / Data Center: clone path is often "scm/PROJ/repo"; drop the
    # leading "scm" segment, then it's "PROJECTKEY/repo".
    parts = [p for p in path.split("/") if p]
    if parts and parts[0].lower() == "scm":
        parts = parts[1:]
    if len(parts) < 2:
        return {"success": False, "error": f"Could not parse Bitbucket project/repo from path: {path}"}
    project_key, repo = parts[0], parts[1]
    api = f"https://{host}/rest/api/1.0/projects/{project_key}/repos/{repo}/pull-requests"
    payload = {
        "title": title,
        "description": body or "",
        "fromRef": {"id": f"refs/heads/{head}"},
        "toRef": {"id": f"refs/heads/{base}"},
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = httpx.post(api, json=payload, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"Bitbucket Server API request failed: {exc}"}
    if resp.status_code in (200, 201):
        data = resp.json()
        links = (data.get("links") or {}).get("self") or []
        url = links[0].get("href") if links else None
        return {"success": True, "url": url, "number": data.get("id"), "provider": "bitbucket"}
    return {"success": False, "error": f"Bitbucket Server API {resp.status_code}: {resp.text[:300]}"}
