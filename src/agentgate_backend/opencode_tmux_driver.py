"""OpenCode agent driver — tmux mode with SQLite output reading.

OpenCode runs as a TUI in tmux. Output is read from OpenCode's SQLite DB
(~/.local/share/opencode/opencode.db) using read-only WAL connections.

tmux_manager is injected at construction for inject/startup operations.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_driver import OutputResult

if TYPE_CHECKING:
    from .tmux_manager import TmuxManager

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

_OPENCODE_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("rate limit", "API rate limited"),
    ("api error", "API error"),
    ("model not found", "Model not found"),
    ("unauthorized", "Auth error"),
    ("quota exceeded", "Quota exceeded"),
    ("connection refused", "Connection refused"),
    ("timeout", "Request timeout"),
]


class OpenCodeTmuxDriver:
    """AgentDriver for OpenCode (tmux + SQLite)."""

    def __init__(
        self,
        tmux_manager: TmuxManager,
        opencode_command: str = "opencode",
        model: str = "",
        work_dir: str = "",
        db_path: Path | None = None,
    ):
        self._tmux = tmux_manager
        self._command = opencode_command
        self._model = model
        self._work_dir = work_dir
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._db: sqlite3.Connection | None = None
        # Pre-load session_id on init so first read_output doesn't falsely
        # detect a "session change" (empty → real_id) and flood IM with history.
        self._session_id: str = self._find_latest_session_id()

    # --- Lifecycle ---

    def get_start_command(self, work_dir: str) -> str:
        parts = [self._command]
        if self._model:
            parts.extend(["-m", self._model])
        return " ".join(parts)

    def build_recovery_command(self, window_name: str) -> str:
        session_id = self._find_latest_session_id()
        base = self.get_start_command(self._work_dir)
        if session_id:
            logger.info("OpenCode driver: resume session %s", session_id)
            return f"{base} --session {session_id}"
        logger.info("OpenCode driver: --continue (no session found)")
        return f"{base} --continue"

    async def accept_startup_prompts(self, window_name: str) -> bool:
        """OpenCode has no trust dialog."""
        return False

    # --- Message flow ---

    async def inject(self, window_name: str, text: str) -> tuple[bool, str]:
        """Inject via tmux send_keys."""
        window = await self._tmux.find_window_by_name(window_name)
        if not window:
            return False, f"Window '{window_name}' not found"
        from .session import session_manager
        return await session_manager.send_to_window(window.window_id, text)

    async def read_output(self, window_name: str, since: int) -> OutputResult:
        """Read new output from OpenCode's SQLite DB.

        since: millisecond timestamp (0 on first call).
        """
        # Always refresh session_id — OC may create a new session at any time
        # (e.g. user starts a new conversation, or session is compacted).
        latest = self._find_latest_session_id()
        if latest:
            if latest != self._session_id:
                logger.info("OpenCode driver: session changed %s → %s", self._session_id, latest)
                self._session_id = latest
                # Seed to the current position in the new session — skip history.
                # Returning cursor=0 would cause the poller to replay ALL content
                # from the new session (including compacted context), flooding IM.
                max_ts = self._get_max_timestamp()
                logger.info("OpenCode driver: seeded new session to ts=%d", max_ts)
                return OutputResult(messages=[], count=0, cursor=max_ts)
            # session unchanged, no need to reassign
        if not self._session_id:
            return OutputResult(messages=[], count=0, cursor=since)

        conn = self._get_db()
        if conn is None:
            return OutputResult(messages=[], count=0, cursor=since)

        try:
            cur = conn.execute(
                """
                SELECT p.time_created, p.data, m.data
                FROM part p
                JOIN message m ON p.message_id = m.id
                WHERE p.session_id = ? AND p.time_created > ?
                ORDER BY p.time_created ASC
                """,
                (self._session_id, since),
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            logger.error("OpenCode driver: SQLite read error: %s", e)
            # Connection may be stale (DB rebuilt). Reset for next call.
            if self._db:
                try:
                    self._db.close()
                except Exception:
                    pass
                self._db = None
            return OutputResult(messages=[], count=0, cursor=since)

        messages = []
        max_ts = since
        _user_skipped = 0
        _none_converted = 0

        for time_created, part_data_str, msg_data_str in rows:
            try:
                part = json.loads(part_data_str)
                msg_meta = json.loads(msg_data_str)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("OC read_output: bad part data at ts=%d: %s", time_created, e)
                if time_created > max_ts:
                    max_ts = time_created
                continue

            # Only emit assistant messages.
            if msg_meta.get("role") == "user":
                _user_skipped += 1
                if time_created > max_ts:
                    max_ts = time_created
                continue

            converted = _convert_part(part, msg_meta.get("role", "assistant"))
            if converted:
                messages.append(converted)
                if time_created > max_ts:
                    max_ts = time_created
            else:
                _none_converted += 1
                # Don't advance cursor past empty text parts — OC writes empty
                # text placeholders first, then updates with real content.
                # If we advance past them, the real content is never seen.
                pt = part.get("type", "?")
                if pt == "text" and not part.get("text"):
                    logger.debug("OC read_output: skipping empty text part at ts=%d (not advancing cursor)", time_created)
                    # Do NOT update max_ts — leave cursor behind this part
                    # so next poll re-reads it after OC fills in the content.
                else:
                    if time_created > max_ts:
                        max_ts = time_created

        # Diagnostic: log every non-trivial read for pipeline tracing
        if rows:
            logger.info(
                "OC read_output: session=%s since=%d → %d rows, %d user-skip, %d none-convert, %d returned, cursor=%d",
                self._session_id[:16] if self._session_id else "?",
                since, len(rows), _user_skipped, _none_converted, len(messages), max_ts,
            )

        if messages:
            logger.info(
                "OC output: session=%s %d messages (cursor %d→%d)",
                self._session_id[:12] if self._session_id else "?", len(messages), since, max_ts,
            )
        return OutputResult(messages=messages, count=len(messages), cursor=max_ts)

    # --- Health ---

    @property
    def process_name(self) -> str:
        # tmux reports "node" as pane_current_command (OC's launcher),
        # not ".opencode" (the actual binary, a child of node).
        return "node"

    @property
    def error_patterns(self) -> list[tuple[str, str]]:
        return _OPENCODE_ERROR_PATTERNS

    # --- Internal ---

    def _get_max_timestamp(self) -> int:
        """Get the latest time_created in the current session (for seeding on session change)."""
        conn = self._get_db()
        if not conn or not self._session_id:
            return 0
        try:
            cur = conn.execute(
                "SELECT MAX(time_created) FROM part WHERE session_id = ?",
                (self._session_id,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else 0
        except sqlite3.Error:
            return 0

    def _get_db(self) -> sqlite3.Connection | None:
        if self._db is not None:
            return self._db
        if not self._db_path.exists():
            logger.warning("OpenCode DB not found at %s", self._db_path)
            return None
        try:
            uri = f"file:{self._db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            # WAL pragma not needed for read-only connections (mode=ro); skip it.
            self._db = conn
            logger.info("OpenCode driver: connected to DB at %s", self._db_path)
            return conn
        except sqlite3.Error as e:
            logger.error("OpenCode driver: DB open failed: %s", e)
            return None

    def _find_latest_session_id(self) -> str:
        conn = self._get_db()
        if conn is None:
            return ""
        try:
            cur = conn.execute(
                "SELECT id FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT 1",
                (self._work_dir,),
            )
            row = cur.fetchone()
            if row:
                return row[0]
        except sqlite3.Error as e:
            logger.error("OpenCode driver: session lookup error: %s", e)
        return ""

    def close(self):
        if self._db:
            self._db.close()
            self._db = None


def _convert_part(part: dict, role: str) -> dict | None:
    """Convert OpenCode part to unified {role, text, content_type} format."""
    pt = part.get("type", "")
    if pt == "text":
        text = part.get("text", "")
        return {"role": role, "text": text, "content_type": "text"} if text else None
    if pt == "tool-call":
        name = part.get("name", "unknown")
        inp = part.get("input", "")
        summary = f"🔧 {name}"
        if isinstance(inp, dict):
            if "command" in inp:
                summary += f"\n$ {str(inp['command'])[:200]}"
            elif "file_path" in inp:
                summary += f"\n📄 {inp['file_path']}"
        elif isinstance(inp, str) and inp:
            summary += f"\n{inp[:200]}"
        return {"role": role, "text": summary, "content_type": "tool_use"}
    if pt == "tool-result":
        output = part.get("output", "")
        if isinstance(output, str) and output:
            return {"role": role, "text": output[:500], "content_type": "tool_result"}
        elif isinstance(output, (dict, list)):
            import json
            return {"role": role, "text": json.dumps(output, ensure_ascii=False)[:500], "content_type": "tool_result"}
    return None
