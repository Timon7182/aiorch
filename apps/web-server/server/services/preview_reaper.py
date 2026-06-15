"""Background reaper for expired preview deploys.

Periodically asks every server that has a preview runner (deploys.preview) to tear
down previews older than the TTL. Keeps disk usage bounded without anyone clicking
"Stop" — important because previews are intentionally disposable.

Runs as a daemon thread (ssh_service/paramiko is blocking). Started/stopped from
the FastAPI lifespan handler.

Env:
  PREVIEW_TTL_HOURS          default 24  — previews older than this are reaped
  PREVIEW_REAP_INTERVAL_MIN  default 30  — sweep cadence
  PREVIEW_REAPER_ENABLED     default 1   — set 0 to disable
"""

from __future__ import annotations

import logging
import os
import threading

from . import ext_storage
from . import preview_deploy_service as pds

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None


def _ttl_hours() -> int:
    try:
        return max(1, int(os.environ.get("PREVIEW_TTL_HOURS", "24")))
    except ValueError:
        return 24


def _interval_seconds() -> int:
    try:
        return max(60, int(os.environ.get("PREVIEW_REAP_INTERVAL_MIN", "30")) * 60)
    except ValueError:
        return 1800


def _sweep() -> None:
    ttl = _ttl_hours()
    for server in ext_storage.load("servers"):
        if not (server.get("deploys") or {}).get(pds.RUNNER_KEY):
            continue
        name = server.get("name") or server.get("id")
        try:
            res = pds.reap(name, ttl)
            reaped = res.get("reaped") or []
            if reaped:
                logger.info("preview reaper: tore down %s on %s", reaped, name)
        except Exception as exc:  # noqa: BLE001 — never let one host break the sweep
            logger.warning("preview reaper failed for %s: %s", name, exc)


def _loop() -> None:
    interval = _interval_seconds()
    # Wait one interval before the first sweep so we don't hammer hosts at boot.
    while not _stop.wait(interval):
        try:
            _sweep()
        except Exception as exc:  # noqa: BLE001
            logger.warning("preview reaper sweep error: %s", exc)


def start() -> None:
    global _thread
    if os.environ.get("PREVIEW_REAPER_ENABLED", "1") == "0":
        logger.info("preview reaper disabled (PREVIEW_REAPER_ENABLED=0)")
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="preview-reaper", daemon=True)
    _thread.start()
    logger.info(
        "preview reaper started (ttl=%dh, every %dmin)",
        _ttl_hours(), _interval_seconds() // 60,
    )


def stop() -> None:
    _stop.set()
