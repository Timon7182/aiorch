"""Per-project preview-deploy config: load, validate, and branch->lane mapping.

A project opts into preview deploys by supplying a `deploy.config.json` (at the
project root or under `.magestic-ai/`). This module loads it, fills in defaults,
and answers "which static lane does this branch belong to".

The shape is documented in apps/web-server/server/deploy/deploy.config.example.json.
The cts (CargoTrackingSystem) shape is the baked-in default so cts works with no
file present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "deploy.config.json"

# Preview strategies:
#   docker-remote  — build the worktree into an isolated Docker stack on a remote
#                    host over SSH (the original, default behavior).
#   dev-server     — run the framework dev server (hot-reload) locally as a
#                    subprocess (npm run dev / manage.py runserver / …).
#   compose-local  — bring the worktree up with a local `docker compose` project.
STRATEGIES = ("docker-remote", "dev-server", "compose-local")
LOCAL_STRATEGIES = ("dev-server", "compose-local")

# Baked-in default — matches cts: .NET backend + Node frontend + Postgres.
DEFAULT_CONFIG: dict[str, Any] = {
    "strategy": "docker-remote",
    "target": "preview-host",
    "ship": "save-load",                       # or "registry"
    "registry": "",
    "exposure": "nginx-port",                  # or "macvlan" | "k3s"
    "domain": "",
    "ttlHours": 24,
    "maxConcurrent": 2,
    "lanes": {
        "A": ["main", "pre-prod", "preprod", "master"],
        "B": ["test"],
    },
    "components": [
        {
            "name": "backend",
            "build": {"dockerfile": "cts-backend/Dockerfile", "context": "cts-backend"},
            "port": 5000,
            "public": False,
            "env": {},
        },
        {
            "name": "frontend",
            "build": {"dockerfile": "frontend/Dockerfile", "context": "frontend"},
            "port": 5000,
            "public": True,
            "env": {},
        },
    ],
    "services": {
        "postgres": {"strategy": "clone", "image": "postgres:16"},
        "minio": {"strategy": "shared"},
    },
}


def config_file(project_path: Path) -> Path | None:
    """Return the path to a project's deploy.config.json if one exists."""
    for candidate in (
        project_path / CONFIG_FILENAME,
        project_path / ".magestic-ai" / CONFIG_FILENAME,
    ):
        if candidate.exists():
            return candidate
    return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_deploy_config(project_path: Path) -> dict[str, Any]:
    """Load a project's deploy config merged over DEFAULT_CONFIG.

    Returns the default config when no file is present (so cts works out of the box).
    `components` from the file fully replace the default list (not merged element-wise).
    """
    path = config_file(project_path)
    if not path:
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        user_cfg = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid {CONFIG_FILENAME}: {exc}") from exc
    if not isinstance(user_cfg, dict):
        raise ValueError(f"{CONFIG_FILENAME} must be a JSON object")
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    # components/lanes are wholesale-replaced when present (avoid half-merged lists)
    if "components" in user_cfg:
        merged["components"] = user_cfg["components"]
    if "lanes" in user_cfg:
        merged["lanes"] = user_cfg["lanes"]
    return merged


def validate_config(config: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems; empty list means OK.

    Validation depends on ``strategy``. The strict docker-remote rules (a
    dockerfile per component, a public component, an exposure/ship/lanes setup)
    only apply to ``docker-remote``. The local strategies (``dev-server``,
    ``compose-local``) are almost entirely auto-detected, so their optional
    config sections are only type-checked when present.
    """
    errors: list[str] = []
    strategy = config.get("strategy", "docker-remote")
    if strategy not in STRATEGIES:
        errors.append("strategy must be one of " + "|".join(STRATEGIES))
        return errors

    if strategy == "dev-server":
        ds = config.get("devServer")
        if ds is not None and not isinstance(ds, dict):
            errors.append("devServer must be an object")
        elif isinstance(ds, dict):
            if ds.get("port") is not None and not isinstance(ds.get("port"), int):
                errors.append("devServer.port must be an integer")
            if ds.get("env") is not None and not isinstance(ds.get("env"), dict):
                errors.append("devServer.env must be an object")
        return errors

    if strategy == "compose-local":
        cf = config.get("composeFile")
        if cf is not None and not isinstance(cf, str):
            errors.append("composeFile must be a string")
        if config.get("port") is not None and not isinstance(config.get("port"), int):
            errors.append("port must be an integer")
        if config.get("containerPort") is not None and not isinstance(config.get("containerPort"), int):
            errors.append("containerPort must be an integer")
        return errors

    # ---- docker-remote (strict) ----
    comps = config.get("components")
    if not isinstance(comps, list) or not comps:
        errors.append("components must be a non-empty array")
    else:
        publics = 0
        for i, c in enumerate(comps):
            if not isinstance(c, dict):
                errors.append(f"components[{i}] must be an object")
                continue
            if not c.get("name"):
                errors.append(f"components[{i}].name is required")
            build = c.get("build") or {}
            if not build.get("dockerfile"):
                errors.append(f"components[{i}].build.dockerfile is required")
            if c.get("public"):
                publics += 1
        if publics == 0:
            errors.append("at least one component must have \"public\": true")
    if config.get("exposure") not in ("nginx-port", "macvlan", "k3s"):
        errors.append("exposure must be one of nginx-port|macvlan|k3s")
    if config.get("ship") not in ("save-load", "registry"):
        errors.append("ship must be one of save-load|registry")
    lanes = config.get("lanes") or {}
    if not isinstance(lanes, dict) or not ({"A", "B"} & set(lanes.keys())):
        errors.append("lanes must define at least A or B")
    return errors


def lane_for_branch(config: dict[str, Any], branch: str) -> str:
    """Map a git branch to a static lane (A or B).

    Exact match first, then prefix match (e.g. feature branches off `test`).
    Defaults to lane A.
    """
    lanes = config.get("lanes") or {}
    branch = (branch or "").strip()
    # exact match
    for lane in ("A", "B"):
        for b in lanes.get(lane, []) or []:
            if branch == b:
                return lane
    # prefix / contains match (feature/test-x -> B if it stems from test)
    for lane in ("B", "A"):  # prefer the more specific lane B
        for b in lanes.get(lane, []) or []:
            if b and (branch.startswith(f"{b}/") or branch.startswith(f"{b}-") or f"/{b}" in branch):
                return lane
    return "A"
