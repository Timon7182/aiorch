"""Lightweight documentation index: scan a markdown tree, store sections in SQLite FTS5.

This is intentionally separate from MagesticAI's Graphiti-powered code memory.
Graphiti is great for "what did the agent learn across sessions" semantics; docs
need a simpler "given query, return ranked markdown sections" surface, and
SQLite FTS5 ships in CPython's bundled sqlite3.

Index location: PROJECTS_DATA_DIR/docs-index/<safe-project-slug>.db
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import get_settings


_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


def _safe(slug: str) -> str:
    out = "".join(c for c in slug if c.isalnum() or c in "-_")
    return out or "default"


def _index_path(project: str) -> Path:
    root = Path(get_settings().PROJECTS_DATA_DIR) / "docs-index"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{_safe(project)}.db"


def _connect(project: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_index_path(project))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(
            file_path, heading, level UNINDEXED, content, line_start UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            file_path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            indexed_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    return conn


@dataclass
class Section:
    file_path: str
    heading: str
    level: int
    content: str
    line_start: int


def _split_sections(text: str, file_path: str) -> list[Section]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [
            Section(
                file_path=file_path,
                heading=Path(file_path).stem,
                level=0,
                content=text,
                line_start=1,
            )
        ]
    sections: list[Section] = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        line_start = text.count("\n", 0, start) + 1
        sections.append(
            Section(
                file_path=file_path,
                heading=heading,
                level=level,
                content=section_text,
                line_start=line_start,
            )
        )
    return sections


def reindex(project: str, root_dir: Path | str) -> dict[str, Any]:
    root = Path(root_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"docs root not found: {root}")

    files = sorted(p for p in root.rglob("*.md") if p.is_file())
    conn = _connect(project)
    try:
        conn.execute("DELETE FROM sections")
        conn.execute("DELETE FROM files")
        total_sections = 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(path.relative_to(root))
            stat = path.stat()
            conn.execute(
                "INSERT INTO files (file_path, mtime, size) VALUES (?, ?, ?)",
                (rel, stat.st_mtime, stat.st_size),
            )
            for sec in _split_sections(text, rel):
                conn.execute(
                    "INSERT INTO sections (file_path, heading, level, content, line_start) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sec.file_path, sec.heading, sec.level, sec.content, sec.line_start),
                )
                total_sections += 1
        conn.commit()
        return {
            "project": project,
            "root": str(root),
            "files_indexed": len(files),
            "sections_indexed": total_sections,
        }
    finally:
        conn.close()


def search(project: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    conn = _connect(project)
    try:
        cur = conn.execute(
            """
            SELECT file_path, heading, level, snippet(sections, 3, '«', '»', '…', 16) AS snippet,
                   bm25(sections) AS score, line_start
            FROM sections
            WHERE sections MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (query, int(limit)),
        )
        return [dict(row) for row in cur]
    finally:
        conn.close()


def stats(project: str) -> dict[str, Any]:
    conn = _connect(project)
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        sections = conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
        last = conn.execute(
            "SELECT MAX(indexed_at) FROM files"
        ).fetchone()[0]
        return {"files": files, "sections": sections, "last_indexed_at": last}
    finally:
        conn.close()
