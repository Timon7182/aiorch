"""Meeting transcripts: stored inside each project at
`<project>/.magestic-ai/transcripts/` so graphify picks them up on its next
`graphify extract` pass.

History: this service originally wrote under `PROJECTS_DATA_DIR/transcripts/`
(the web-server's own data dir). That kept pasted transcripts out of reach of
graphify, which only scans the project tree. We now converge on the same
directory used by the multipart audio/video ingest route in
`routes/transcripts.py`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .project_resolve import resolve_project_dir


def _project_transcripts_dir(project_slug: str) -> Path:
    """Resolve slug → `<project>/.magestic-ai/transcripts/`, creating it if missing.

    Raises LookupError if the slug doesn't match a registered project. Callers
    in `routes/extensions.py` translate that into an HTTP 404.
    """
    project_path = resolve_project_dir(project_slug)
    if project_path is None:
        raise LookupError(project_slug)
    d = project_path / ".magestic-ai" / "transcripts"
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
    transcripts_dir = _project_transcripts_dir(project)
    path = transcripts_dir / filename

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
        "filename": filename,
        "path": str(Path(".magestic-ai") / "transcripts" / filename),
        "content_hash": digest,
        "occurred_at": ts,
        "bytes": path.stat().st_size,
    }


def list_for(project: str) -> list[dict[str, Any]]:
    try:
        transcripts_dir = _project_transcripts_dir(project)
    except LookupError:
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(transcripts_dir.glob("*.md")):
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
    transcripts_dir = _project_transcripts_dir(project)
    safe = Path(filename).name
    p = transcripts_dir / safe
    if not p.exists():
        raise FileNotFoundError(filename)
    return p.read_text(encoding="utf-8")
