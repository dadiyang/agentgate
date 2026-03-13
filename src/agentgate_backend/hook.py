"""Claude Code session hook — captures session_id to session_map.json.

Called by Claude Code via `--hook` mechanism when sessions start or update.
Writes a mapping of tmux window (by cwd) to session_id so the backend can
resolve which session_id belongs to which tmux window.

Usage (set in Claude Code config):
    claude --hook /path/to/hook.py

The hook receives a JSON event as argv[1]:
    {
        "session_id": "abc123",
        "cwd": "/home/user/myproject",
        ...
    }

The AGENTGATE_DIR env var overrides the default data directory (~/.agentgate).
"""

import json
import logging
import os
import sys
from pathlib import Path

from .utils import atomic_write_json

logger = logging.getLogger(__name__)


def main() -> None:
    """Entry point called by Claude Code hook system.

    Expects a JSON event as first argument with session_id and cwd fields.
    Updates session_map.json mapping cwd paths to session IDs.
    """
    if len(sys.argv) < 2:
        return

    try:
        event = json.loads(sys.argv[1])
    except (json.JSONDecodeError, IndexError):
        return

    session_id = event.get("session_id")
    cwd = event.get("cwd", "")
    if not session_id:
        return

    # Resolve data directory: AGENTGATE_DIR env var takes precedence
    data_dir = Path(
        os.environ.get("AGENTGATE_DIR", str(Path.home() / ".agentgate"))
    )
    map_path = data_dir / "session_map.json"

    try:
        existing = json.loads(map_path.read_text()) if map_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        existing = {}

    # Map by cwd so the backend can resolve window→session via working directory
    if cwd:
        existing[cwd] = session_id

    # Also store a keyed entry for direct session_id lookup
    existing[f"session:{session_id}"] = {"cwd": cwd, "session_id": session_id}

    atomic_write_json(map_path, existing)
    logger.debug("Updated session_map: cwd=%s session_id=%s", cwd, session_id)


if __name__ == "__main__":
    main()
