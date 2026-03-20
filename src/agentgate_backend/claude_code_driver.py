"""Claude Code agent driver — wraps existing session/monitor/parser logic.

Delegates to existing session.py, session_monitor.py, and transcript_parser.py.
tmux_manager and session_manager are injected at construction, not exposed
in the AgentDriver protocol.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_driver import OutputResult
from .config import config

if TYPE_CHECKING:
    from .session import SessionManager
    from .tmux_manager import TmuxManager

logger = logging.getLogger(__name__)

_CLAUDE_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("rate limit", "API rate limited"),
    ("overloaded", "API overloaded"),
    ("run /login", "Auth expired"),
    ("not logged in", "Auth expired"),
    ("econnrefused", "Connection refused"),
    ("etimedout", "Connection timed out"),
    ("api error", "API error"),
    ("account does not have", "Account issue"),
]


class ClaudeCodeDriver:
    """AgentDriver for Claude Code (tmux + JSONL)."""

    def __init__(
        self,
        claude_command: str,
        session_manager: SessionManager,
        tmux_manager: TmuxManager,
    ):
        self._claude_command = claude_command
        self._session_mgr = session_manager
        self._tmux = tmux_manager

    # --- Lifecycle ---

    def get_start_command(self, work_dir: str) -> str:
        return self._claude_command

    def build_recovery_command(self, window_name: str) -> str:
        session_id = self._get_session_id_from_map(window_name)
        if session_id:
            logger.info("CC driver: --resume %s for window %s", session_id, window_name)
            return f"{self._claude_command} --resume {session_id}"
        logger.info("CC driver: --continue for window %s", window_name)
        return f"{self._claude_command} --continue"

    async def accept_startup_prompts(self, window_name: str) -> bool:
        window = await self._tmux.find_window_by_name(window_name)
        if not window:
            return False
        return await self._tmux._accept_trust_dialog(window.window_id)

    # --- Message flow ---

    async def inject(self, window_name: str, text: str) -> tuple[bool, str]:
        window = await self._tmux.find_window_by_name(window_name)
        if not window:
            return False, f"Window '{window_name}' not found"
        return await self._session_mgr.send_to_window(window.window_id, text)

    async def read_output(self, window_name: str, since: int) -> OutputResult:
        window = await self._tmux.find_window_by_name(window_name)
        if not window:
            return OutputResult(messages=[], count=0, cursor=since)
        await self._session_mgr.load_session_map()
        messages, count = await self._session_mgr.get_recent_messages(
            window.window_id, start_byte=since,
        )
        session = await self._session_mgr.resolve_session_for_window(window.window_id)
        next_offset = since
        if session and session.file_path:
            try:
                next_offset = Path(session.file_path).stat().st_size
            except OSError:
                pass
        return OutputResult(messages=messages, count=count, cursor=next_offset)

    # --- Health ---

    @property
    def process_name(self) -> str:
        return config.process_name

    @property
    def error_patterns(self) -> list[tuple[str, str]]:
        return _CLAUDE_ERROR_PATTERNS

    # --- Internal ---

    def _get_session_id_from_map(self, window_id: str) -> str | None:
        session_map_file = config.session_map_file
        if not session_map_file.exists():
            return None
        try:
            data = json.loads(session_map_file.read_text())
            key = f"{config.tmux_session_name}:{window_id}"
            info = data.get(key, {})
            sid = info.get("session_id", "")
            return sid if sid else None
        except (json.JSONDecodeError, OSError):
            return None
