"""Automatic window recovery when claude dies or window disappears.

Recreates tmux window, starts claude with --resume {session_id} (from
session_map.json) or falls back to --continue if no session_id is known.
Updates display names. Stops retrying after max_attempts consecutive failures.

Recovery command priority:
  1. `claude --resume {session_id}` — from session_map.json (exact session)
  2. `claude --continue` — fallback when no session_map entry exists
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from .config import config as _backend_config

if TYPE_CHECKING:
    from .session import SessionManager
    from .tmux_manager import TmuxManager

logger = logging.getLogger(__name__)


class WindowRecovery:
    """Recover tmux windows when claude dies or window disappears.

    Uses session_map.json to find the correct session_id for --resume,
    so claude resumes exactly the right session rather than blindly picking
    the most recently modified JSONL in the CWD (which --continue does).
    """

    def __init__(
        self,
        *,
        tmux_manager: TmuxManager,
        session_manager: SessionManager,
        claude_command: str | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._tmux = tmux_manager
        self._session_mgr = session_manager
        self._claude_command = claude_command or _backend_config.claude_command
        self.max_attempts = max_attempts
        self._attempt_counts: dict[str, int] = {}  # window_id -> consecutive failures
        self._recovering: set[str] = set()  # window_ids currently recovering

    def _get_session_id_from_map(self, window_id: str) -> str | None:
        """Read session_map.json and look up session_id for a window_id.

        Key format: "{tmux_session_name}:{window_id}"
        Returns session_id string if found, None otherwise.
        """
        session_map_file = _backend_config.session_map_file
        if not session_map_file.exists():
            return None
        try:
            data = json.loads(session_map_file.read_text())
            key = f"{_backend_config.tmux_session_name}:{window_id}"
            info = data.get(key, {})
            sid = info.get("session_id", "")
            return sid if sid else None
        except (json.JSONDecodeError, OSError):
            return None

    def _build_recovery_command(self, window_id: str) -> str:
        """Build the recovery command for a window.

        Priority 1: `{claude_command} --resume {session_id}` — from session_map.json
        Priority 2: `{claude_command} --continue` — fallback

        Using --resume prevents claude from picking the wrong session when
        there are multiple JSONL files in the same CWD directory.
        """
        session_id = self._get_session_id_from_map(window_id)
        if session_id:
            logger.info(
                "WindowRecovery: using --resume %s for window %s",
                session_id, window_id,
            )
            return f"{self._claude_command} --resume {session_id}"

        logger.info(
            "WindowRecovery: no session_id in session_map for window %s, "
            "falling back to --continue",
            window_id,
        )
        return f"{self._claude_command} --continue"

    async def recover_window(self, old_window_id: str) -> dict:
        """Attempt to recover a dead/missing window.

        Returns dict with:
          success: bool
          new_window_id: str (if success)
          context_restored: bool (if success — True when session_map updated)
          error: str (if failure)
        """
        # Prevent concurrent recovery for same window
        if old_window_id in self._recovering:
            return {"success": False, "error": "recovery_in_progress"}
        self._recovering.add(old_window_id)

        try:
            return await self._do_recover(old_window_id)
        finally:
            self._recovering.discard(old_window_id)

    async def _do_recover(self, old_window_id: str) -> dict:
        # Check attempt limit
        attempts = self._attempt_counts.get(old_window_id, 0)
        if attempts >= self.max_attempts:
            logger.error(
                "Recovery stopped for %s: %d/%d attempts exhausted",
                old_window_id, attempts, self.max_attempts,
            )
            return {"success": False, "error": "max_attempts_exceeded"}

        # Get CWD from persisted state
        ws = self._session_mgr.window_states.get(old_window_id)
        if not ws or not ws.cwd:
            self._attempt_counts[old_window_id] = attempts + 1
            return {"success": False, "error": "no_cwd_for_window"}

        cwd = ws.cwd
        logger.info(
            "Recovering window %s (cwd=%s, attempt %d/%d)",
            old_window_id, cwd, attempts + 1, self.max_attempts,
        )

        # Check if window still exists (maybe just claude died)
        existing = await self._tmux.find_window_by_id(old_window_id)
        if existing and existing.pane_current_command == _backend_config.process_name:
            # Window exists and claude is running — nothing to recover
            self._attempt_counts.pop(old_window_id, None)
            return {"success": True, "new_window_id": old_window_id,
                    "note": "already_running"}

        new_window_id = old_window_id
        if not existing:
            # Window is gone — create new one
            window_name = self._session_mgr.get_display_name(old_window_id)
            ok, msg, wname, wid = await self._tmux.create_window(
                cwd, window_name=window_name, start_claude=False,
            )
            if not ok:
                self._attempt_counts[old_window_id] = attempts + 1
                logger.error("Window creation failed: %s", msg)
                return {"success": False, "error": f"create_failed: {msg}"}
            new_window_id = wid

        # Start claude with session-aware recovery command
        recovery_cmd = self._build_recovery_command(old_window_id)
        await self._tmux.send_keys(new_window_id, recovery_cmd, enter=True, literal=False)

        # Accept trust dialog if it appears
        await self._tmux._accept_trust_dialog(new_window_id)

        # Check if claude started successfully
        window = await self._tmux.find_window_by_id(new_window_id)
        if not window or window.pane_current_command != _backend_config.process_name:
            # Primary recovery command failed, try plain --continue as last resort
            logger.warning(
                "Primary recovery command failed for %s, trying --continue",
                new_window_id,
            )
            fallback_cmd = f"{self._claude_command} --continue"
            await self._tmux.send_keys(
                new_window_id, fallback_cmd, enter=True, literal=False,
            )
            await self._tmux._accept_trust_dialog(new_window_id)
            window = await self._tmux.find_window_by_id(new_window_id)
            if not window or window.pane_current_command != _backend_config.process_name:
                self._attempt_counts[old_window_id] = attempts + 1
                return {"success": False, "error": "claude_start_failed"}

        # Wait up to 15 seconds for session_map.json to update (confirms claude started)
        got_session = await self._session_mgr.wait_for_session_map_entry(
            new_window_id, timeout=15.0,
        )

        # Update display name mapping if window_id changed
        if new_window_id != old_window_id:
            self._update_display_name(old_window_id, new_window_id)

        # Success — reset attempt counter
        self._attempt_counts.pop(old_window_id, None)

        logger.info(
            "Window recovered: %s -> %s (context_restored=%s)",
            old_window_id, new_window_id, got_session,
        )
        return {
            "success": True,
            "new_window_id": new_window_id,
            "context_restored": got_session,
        }

    def _update_display_name(self, old_wid: str, new_wid: str) -> None:
        """Transfer display name from old to new window_id."""
        name = self._session_mgr.get_display_name(old_wid)
        if name and name != old_wid:
            self._session_mgr.set_display_name(new_wid, name)
