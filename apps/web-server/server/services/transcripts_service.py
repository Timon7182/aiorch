"""Meeting transcripts: file-based storage under PROJECTS_DATA_DIR/transcripts/."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import get_settings


def _root() -> Path:
    root = Path(get_settings().PROJECTS_DATA_DIR) / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_dir(project_slug: str) -> Path:
    safe = "".join(c for c in project_slug if c.isalnum() or c in "-_") or "default"
    d = _root() / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def store(
    *,
    project: str,
    title: str,
    content: str,
    occurred_at: str | None = None,
    participants: list[str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    ts = occurred_at or datetime.now(timezone.utc).isoformat()
    safe_title = "".join(c for c in title if c.isalnum() or c in "-_ ").strip().replace(" ", "_") or "untitled"
    filename = f"{ts[:10]}_{safe_title}_{digest[:8]}.md"
    path = _project_dir(project) / filename

    front_matter = (
        "---\n"
        f"title: {title}\n"
        f"project: {project}\n"
        f"occurred_at: {ts}\n"
        f"participants: {participants or []}\n"
        f"source: {source or ''}\n"
        f"content_hash: {digest}\n"
        "---\n\n"
    )
    path.write_text(front_matter + content, encoding="utf-8")
    return {
        "project": project,
        "title": title,
        "path": str(path.relative_to(_root())),
        "content_hash": digest,
        "occurred_at": ts,
        "bytes": path.stat().st_size,
    }


def list_for(project: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(_project_dir(project).glob("*.md")):
        stat = p.stat()
        out.append(
            {
                "filename": p.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return out


def read(project: str, filename: str) -> str:
    safe = Path(filename).name
    p = _project_dir(project) / safe
    if not p.exists():
        raise FileNotFoundError(filename)
    return p.read_text(encoding="utf-8")
