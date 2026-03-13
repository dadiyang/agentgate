"""Heartbeat file writer -- external watchdog reads this to detect ccbot hangs."""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_started_at: str | None = None


def write_heartbeat(
    path: Path,
    metrics: dict | None = None,
    status: str = "running",
) -> None:
    """Write heartbeat atomically (tmp + rename)."""
    global _started_at
    now = datetime.now(timezone.utc).isoformat()
    if _started_at is None:
        _started_at = now

    data = {
        "service": "ccbot",
        "pid": os.getpid(),
        "started_at": _started_at,
        "heartbeat": now,
        "status": status,
        "metrics": metrics or {},
    }

    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except Exception as e:
        logger.error("Failed to write heartbeat: %s", e, exc_info=True)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def read_heartbeat(path: Path) -> dict | None:
    """Read heartbeat. Returns None if missing or corrupt."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Failed to read heartbeat from %s: %s", path, e)
        return None
