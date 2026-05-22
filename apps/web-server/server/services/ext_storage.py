"""Tiny JSON-on-disk storage for the extension features (servers, dbs, transcripts).

Lives under PROJECTS_DATA_DIR/connections/ to keep it co-located with the rest
of MagesticAI's persistent data. Intentionally simple — no migration story, no
encryption. Secrets are stored in plaintext; tag this clearly and harden later.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import get_settings

_LOCKS: dict[str, threading.Lock] = {}


def _data_root() -> Path:
    root = Path(get_settings().PROJECTS_DATA_DIR) / "connections"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _file(collection: str) -> Path:
    return _data_root() / f"{collection}.json"


def _lock(collection: str) -> threading.Lock:
    if collection not in _LOCKS:
        _LOCKS[collection] = threading.Lock()
    return _LOCKS[collection]


def load(collection: str) -> list[dict[str, Any]]:
    path = _file(collection)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save(collection: str, items: list[dict[str, Any]]) -> None:
    path = _file(collection)
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def insert(collection: str, item: dict[str, Any]) -> dict[str, Any]:
    with _lock(collection):
        items = load(collection)
        item.setdefault("id", str(uuid4()))
        items.append(item)
        save(collection, items)
        return item


def update(collection: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _lock(collection):
        items = load(collection)
        for i, existing in enumerate(items):
            if existing.get("id") == item_id:
                items[i] = {**existing, **patch, "id": item_id}
                save(collection, items)
                return items[i]
    return None


def delete(collection: str, item_id: str) -> bool:
    with _lock(collection):
        items = load(collection)
        new = [it for it in items if it.get("id") != item_id]
        if len(new) == len(items):
            return False
        save(collection, new)
        return True


def find(collection: str, item_id: str) -> dict[str, Any] | None:
    for it in load(collection):
        if it.get("id") == item_id:
            return it
    return None
