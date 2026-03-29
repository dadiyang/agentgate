"""Claude Code subprocess driver — uses CC's stream-json mode.

Runs `claude -p --input-format stream-json --output-format stream-json`
as a long-running subprocess.  Messages injected via stdin, output read
from stdout.  No tmux required.

Verified behavior (2026-03-19):
  - Process stays alive across multiple turns
  - Context retained between messages
  - Streaming output (events arrive in real-time)
  - Agent busy: new messages ignored by CC, driver queues them
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .agent_driver import OutputResult
from .config import config

logger = logging.getLogger(__name__)

_MAX_OUTPUT_ENTRIES = 2000


class ClaudeCodeSubprocessDriver:
    """AgentDriver for Claude Code via subprocess stream-json mode."""

    def __init__(
        self,
        claude_command: str = "claude",
        work_dir: str = "",
        model: str = "",
        permission_mode: str = "bypassPermissions",
    ):
        self._command = claude_command
        self._work_dir = work_dir
        self._model = model
        self._permission_mode = permission_mode

        # Process state
        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str = ""
        self._output: list[dict] = []
        self._output_bytes: int = 0
        self._busy = False
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._start_lock = asyncio.Lock()

        # Persist session_id to survive backend restarts
        self._session_file = config.instance_dir / "cc_subprocess_session_id"
        self._load_session_id()

    # --- AgentDriver protocol ---

    def get_start_command(self, work_dir: str) -> str:
        return self._command

    def build_recovery_command(self, window_name: str) -> str:
        return self._command

    async def accept_startup_prompts(self, window_name: str) -> bool:
        return False  # -p mode skips all prompts

    async def inject(self, window_name: str, text: str) -> tuple[bool, str]:
        """Send message to CC via stdin pipe."""
        if self._proc is None or self._proc.returncode is not None:
            await self._start_process()

        if self._busy:
            await self._queue.put(text)
            return True, "Queued"

        self._busy = True
        self._append_output("user", text)
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text},
        })
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write((msg + "\n").encode())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.error("CC subprocess: stdin write failed (%s), restarting process", e)
            self._busy = False
            if not hasattr(self, '_inject_retry'):
                self._inject_retry = True
                await self._start_process()
                try:
                    return await self.inject(window_name, text)
                finally:
                    del self._inject_retry
            else:
                return False, "Process restart failed"
        return True, "Injected"

    async def read_output(self, window_name: str, since: int) -> OutputResult:
        messages = []
        offset = 0
        for msg in self._output:
            size = msg.get("_size", 100)
            if offset >= since:
                messages.append({k: v for k, v in msg.items() if not k.startswith("_")})
            offset += size
        return OutputResult(messages=messages, count=len(messages), cursor=self._output_bytes)

    @property
    def process_name(self) -> str:
        return "claude"

    @property
    def error_patterns(self) -> list[tuple[str, str]]:
        return [
            ("rate limit", "API rate limited"),
            ("overloaded", "API overloaded"),
            ("run /login", "Auth expired"),
        ]

    def close(self):
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
        if self._reader_task:
            self._reader_task.cancel()

    # --- Session persistence ---

    def _load_session_id(self):
        """Load session_id from disk (survives backend restart)."""
        try:
            if self._session_file.exists():
                sid = self._session_file.read_text().strip()
                if sid:
                    self._session_id = sid
                    logger.info("CC subprocess: loaded session_id=%s from disk", sid)
        except Exception as e:
            logger.error("CC subprocess: failed to load session_id: %s", e)

    def _save_session_id(self, session_id: str):
        """Persist session_id to disk."""
        try:
            self._session_file.write_text(session_id)
            logger.info("CC subprocess: saved session_id=%s to disk", session_id)
        except Exception as e:
            logger.error("CC subprocess: failed to save session_id: %s", e)

    # --- Internal ---

    async def _start_process(self):
        """Start the CC stream-json subprocess.

        If a previous session_id exists, uses --resume to restore context.
        """
        async with self._start_lock:
            args = [
                self._command, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--permission-mode", self._permission_mode,
            ]
            if self._session_id:
                args.extend(["--resume", self._session_id])
                logger.info("CC subprocess: resuming session %s", self._session_id)
            if self._model:
                args.extend(["--model", self._model])

            logger.info("CC subprocess: starting %s", " ".join(args[:8]))
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self._work_dir or None,
                env={**os.environ},
            )
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        """Read stdout events from CC process."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for line_bytes in self._proc.stdout:
                line = line_bytes.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning("CC subprocess: JSON parse error on stdout line: %s | line=%r", e, line[:200])
                    continue
                self._handle_event(event)
        except Exception as e:
            logger.error("CC subprocess read error: %s", e, exc_info=True)
        finally:
            # Process exited (normal or crash) — ensure busy is cleared
            self._busy = False
            # Drain queued messages by restarting process for next one
            if not self._queue.empty():
                text = self._queue.get_nowait()
                task = asyncio.create_task(self.inject("", text))
                task.add_done_callback(
                    lambda t: t.exception() and logger.error(
                        "CC subprocess: queue drain task failed: %s", t.exception()
                    )
                )

    def _handle_event(self, event: dict):
        etype = event.get("type", "")

        if etype == "assistant":
            for c in event.get("message", {}).get("content", []):
                if c.get("type") == "text":
                    self._append_output("assistant", c["text"])
                elif c.get("type") == "tool_use":
                    name = c.get("name", "unknown")
                    self._append_output("assistant", f"🔧 {name}", "tool_use")

        elif etype == "result":
            new_sid = event.get("session_id", "")
            if new_sid and new_sid != self._session_id:
                self._session_id = new_sid
                self._save_session_id(new_sid)
            self._busy = False
            # Process queued message
            if not self._queue.empty():
                text = self._queue.get_nowait()
                task = asyncio.create_task(self.inject("", text))
                task.add_done_callback(
                    lambda t: t.exception() and logger.error(
                        "CC subprocess: queue drain task failed: %s", t.exception()
                    )
                )

    def _append_output(self, role: str, text: str, content_type: str = "text"):
        msg = {"role": role, "text": text, "content_type": content_type,
               "_size": len(text.encode()) + 50}
        self._output.append(msg)
        self._output_bytes += msg["_size"]
        # Trim oldest half when buffer exceeds max size (_output_bytes stays monotonic)
        if len(self._output) > _MAX_OUTPUT_ENTRIES:
            trim_count = _MAX_OUTPUT_ENTRIES // 2
            self._output = self._output[trim_count:]
