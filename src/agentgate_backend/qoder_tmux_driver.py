"""Qoder CLI agent driver — tmux mode with JSONL session reading.

Qoder CLI stores sessions at:
  ~/.qoder/projects/<encoded-cwd>/<session_id>.jsonl

The encoding is identical to Claude Code (/ → -), so SessionManager can be
reused with a different projects_root.

Session tracking: no hook mechanism exists for Qoder, so QoderTmuxDriver
scans the project dir for the most recently modified JSONL on each read.
The latest file is cached and only re-scanned when stale (file disappears or
a newer file appears).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_driver import OutputResult
from .config import config

if TYPE_CHECKING:
    from .tmux_manager import TmuxManager

logger = logging.getLogger(__name__)

_QODER_PROJECTS_ROOT = Path.home() / ".qoder" / "projects"

_QODER_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("rate limit", "API rate limited"),
    ("unauthorized", "Auth error"),
    ("api error", "API error"),
    ("model not found", "Model not found"),
]


class QoderTmuxDriver:
    """AgentDriver for Qoder CLI (tmux + JSONL)."""

    def __init__(
        self,
        tmux_manager: TmuxManager,
        qoder_command: str = "qodercli",
        work_dir: str = "",
        model: str = "",
        yolo: bool = False,
    ):
        self._tmux = tmux_manager
        self._command = qoder_command
        self._work_dir = work_dir
        self._model = model
        self._yolo = yolo

        # Cached current session file — refreshed lazily
        self._session_file: Path | None = None
        self._session_id: str = ""

    # --- Lifecycle ---

    def get_start_command(self, work_dir: str) -> str:
        cmd = self._command
        if self._model:
            cmd += f" --model {self._model}"
        if self._yolo:
            cmd += " --yolo"
        return cmd

    def build_recovery_command(self, window_id: str) -> str:
        # Re-scan for latest session on recovery
        session_file = self._find_latest_session()
        if session_file:
            sid = session_file.stem
            logger.info(
                "Qoder tmux: recovery --resume %s for window %s", sid, window_id
            )
            return f"{self._command} -r {sid}"
        logger.info(
            "Qoder tmux: no session found, starting fresh for window %s", window_id
        )
        return self._command

    async def accept_startup_prompts(self, window_id: str) -> bool:
        return False  # Qoder CLI has no trust dialogs

    # --- Message flow ---

    async def inject(self, window_name: str, text: str) -> tuple[bool, str]:
        window = await self._tmux.find_window_by_name(window_name)
        if not window:
            return False, f"Window '{window_name}' not found"
        ok = await self._tmux.send_keys(
            window.window_id, text, enter=True, literal=True
        )
        return ok, "Injected" if ok else "send_keys failed"

    async def read_output(self, window_name: str, since: int) -> OutputResult:
        session_file = self._get_session_file()
        if not session_file:
            logger.debug("Qoder tmux: no session file found for cwd=%s", self._work_dir)
            return OutputResult(messages=[], count=0, cursor=since)

        try:
            file_size = session_file.stat().st_size
        except OSError as e:
            logger.warning("Qoder tmux: session file disappeared: %s", e)
            self._session_file = None
            return OutputResult(messages=[], count=0, cursor=since)

        if since >= file_size:
            return OutputResult(messages=[], count=0, cursor=file_size)

        try:
            raw = session_file.read_bytes()[since:]
        except OSError as e:
            logger.error(
                "Qoder tmux: failed to read session file: %s", e, exc_info=True
            )
            return OutputResult(messages=[], count=0, cursor=since)

        messages = []
        for line in raw.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(
                    "Qoder tmux: JSONL parse error: %s | line=%r", e, line[:100]
                )
                continue
            parsed = _parse_entry(entry)
            messages.extend(parsed)

        return OutputResult(messages=messages, count=len(messages), cursor=file_size)

    # --- Health ---

    @property
    def process_name(self) -> str:
        return config.process_name

    @property
    def error_patterns(self) -> list[tuple[str, str]]:
        return _QODER_ERROR_PATTERNS

    # --- Internal: session file discovery ---

    def _get_project_dir(self) -> Path:
        """Encode work_dir the same way Qoder does: / → -."""
        encoded = str(Path(self._work_dir).resolve()).replace("/", "-")
        return _QODER_PROJECTS_ROOT / encoded

    def _find_latest_session(self) -> Path | None:
        """Return the most recently modified JSONL in the project dir."""
        project_dir = self._get_project_dir()
        if not project_dir.exists():
            return None
        files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None

    def _get_session_file(self) -> Path | None:
        """Return cached session file, refreshing if a newer one exists."""
        latest = self._find_latest_session()
        if not latest:
            return None

        # Switch to newer file if session has changed
        if self._session_file != latest:
            logger.info(
                "Qoder tmux: session file %s → %s",
                self._session_file.name if self._session_file else "none",
                latest.name,
            )
            self._session_file = latest
            self._session_id = latest.stem

        return self._session_file


# --- JSONL parser ---


def _parse_entry(entry: dict) -> list[dict]:
    """Parse one Qoder JSONL entry into gateway-compatible message dicts."""
    etype = entry.get("type", "")
    message = entry.get("message", {})
    content = message.get("content", [])

    if etype not in ("assistant", "user"):
        return []

    results = []
    for part in content:
        ptype = part.get("type", "")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                results.append({"role": etype, "text": text, "content_type": "text"})
        elif ptype == "thinking":
            text = part.get("thinking", "")
            if text:
                results.append(
                    {"role": etype, "text": text, "content_type": "thinking"}
                )
        elif ptype == "tool_use":
            name = part.get("name", "unknown")
            inp = part.get("input", {})
            summary = f"🔧 {name}"
            if isinstance(inp, dict):
                if "command" in inp:
                    summary += f"\n$ {inp['command'][:200]}"
                elif "file_path" in inp:
                    summary += f"\n📄 {inp['file_path']}"
            results.append({"role": etype, "text": summary, "content_type": "tool_use"})

    return results
