#!/usr/bin/env python3
"""agentgate Stop hook — writes session_map.json when Claude Code stops.

Registered in ~/.claude/settings.json Stop hook so it runs after every
Claude Code session ends. Reads the Stop event from stdin, finds the
most-specific matching agentgate backend by cwd, and writes the session
binding to session_map.json so the backend's session_monitor can locate
the JSONL file.

Registration in ~/.claude/settings.json:
    "hooks": {
        "Stop": [{"hooks": [{"type": "command",
                              "command": "python3 /path/to/agentgate-session-hook.py",
                              "timeout": 5}]}]
        ...
    }

Session map key format: "<tmux_session_name>:<window_id>" (e.g. "agentgate-myproj:@12")
Value format: {"session_id": "...", "cwd": "...", "window_name": "..."}
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[agentgate-hook] {msg}", file=sys.stderr)


def _atomic_write_json(path: Path, data: dict) -> None:
    # Same logic as agentgate_backend.utils.atomic_write_json —
    # kept inline because this script must run without the package installed.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip("\"'")
    return result


def _find_tmux_window_by_name(window_name: str, tmux_session: str) -> str | None:
    """Return window_id for the window matching window_name in tmux_session.

    Matches by window name (reliable) instead of pane_current_path (unreliable
    — pane cwd can drift when CC runs shell commands during a session).
    """
    try:
        r = subprocess.run(
            ["tmux", "list-windows", "-t", tmux_session,
             "-F", "#{window_id}|#{window_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        for line in r.stdout.strip().splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[1] == window_name:
                return parts[0]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _find_best_backend(
    cwd: str, backends_dir: Path
) -> tuple[Path, dict[str, str]] | None:
    """Return (backend_dir, env_vars) for the most-specific backend matching cwd.

    "Most specific" = longest AGENTGATE_WORK_DIR that is a prefix of cwd.
    Using longest-match prevents a parent-dir backend from receiving session
    bindings that belong to a child-dir backend.
    """
    best_dir: Path | None = None
    best_env: dict[str, str] = {}
    best_len = -1

    for backend_dir in backends_dir.iterdir():
        if not backend_dir.is_dir():
            continue
        env_file = backend_dir / ".env"
        if not env_file.exists():
            continue

        try:
            env_vars = _parse_env_file(env_file)
        except OSError:
            continue

        work_dir = env_vars.get("AGENTGATE_WORK_DIR", "").rstrip("/")
        if not work_dir:
            continue

        if cwd == work_dir or cwd.startswith(work_dir + "/"):
            if len(work_dir) > best_len:
                best_dir = backend_dir
                best_env = env_vars
                best_len = len(work_dir)

    if best_dir is None:
        return None
    return best_dir, best_env


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        return

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        _log("failed to parse stdin JSON")
        return

    session_id: str = event.get("session_id", "")
    cwd: str = event.get("cwd", "")
    if not session_id or not cwd:
        return

    backends_dir = Path(
        os.environ.get("AGENTGATE_DATA_DIR",
                       str(Path.home() / ".agentgate" / "backends"))
    )
    if not backends_dir.exists():
        return

    result = _find_best_backend(cwd, backends_dir)
    if result is None:
        _log(f"no backend matched cwd={cwd}")
        return

    backend_dir, env_vars = result
    work_dir = env_vars.get("AGENTGATE_WORK_DIR", "").rstrip("/")
    tmux_session = env_vars.get(
        "AGENTGATE_TMUX_SESSION_NAME", f"agentgate-{backend_dir.name}"
    )
    # Window name is always the basename of work_dir (agentgate convention)
    expected_window_name = Path(work_dir).name

    window_id = _find_tmux_window_by_name(expected_window_name, tmux_session)
    if window_id is None:
        _log(
            f"tmux window '{expected_window_name}' not found in session "
            f"'{tmux_session}' — session binding not written"
        )
        return

    key = f"{tmux_session}:{window_id}"
    map_path = backend_dir / "session_map.json"
    try:
        existing = json.loads(map_path.read_text()) if map_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        existing = {}

    existing[key] = {
        "session_id": session_id,
        "cwd": cwd,
        "window_name": expected_window_name,
    }
    _atomic_write_json(map_path, existing)
    _log(f"wrote session_map: {key} -> session_id={session_id} cwd={cwd}")


if __name__ == "__main__":
    main()
