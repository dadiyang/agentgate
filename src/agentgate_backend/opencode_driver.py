"""OpenCode agent driver — subprocess-based integration.

Manages OpenCode via `opencode run --format json --session <id>` subprocess calls.
Each user message spawns a new process; multi-turn continuity is maintained via
the --session flag.  Output is buffered in-memory and served via the same
/api/output contract as the Claude Code (tmux) backend.

Key design choices:
  - One process per user message (opencode run is one-shot, not long-running)
  - Session ID extracted from step_start NDJSON event, persisted to disk
  - Message queue: if agent is busy, new messages wait for step_finish
  - Output buffer: append-only list with byte-offset simulation for gateway compat
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import config

logger = logging.getLogger(__name__)


# Output message format (matches what gateway expects from CC backend)
@dataclass
class OutputMessage:
    role: str  # "user" | "assistant"
    text: str
    content_type: str = "text"  # "text" | "thinking" | "tool_use"


@dataclass
class OpenCodeDriverState:
    """Persistent state for the OpenCode driver."""

    session_id: str = ""
    total_turns: int = 0


class OpenCodeDriver:
    """Drives OpenCode via subprocess for agentgate-backend."""

    def __init__(
        self,
        work_dir: str,
        opencode_cmd: str = "opencode",
        model: str = "",
    ):
        self._work_dir = work_dir
        self._cmd = opencode_cmd
        self._model = model

        # Output buffer: list of dicts in gateway-compatible format
        self._output: list[dict] = []
        self._output_bytes: int = 0  # Simulated byte offset

        # Session state
        self._session_id: str = ""
        self._state_file = config.instance_dir / "opencode_state.json"
        self._load_state()

        # Process management
        self._busy = False
        self._queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        # (text, message_id)
        self._current_proc: asyncio.subprocess.Process | None = None
        self._last_error: str | None = None
        self._last_activity: float = time.monotonic()

    # ------------------------------------------------------------------
    # Public API (called by inject_server)
    # ------------------------------------------------------------------

    async def inject(self, text: str, message_id: str | None = None) -> dict:
        """Inject a user message. Returns immediately; processing is async."""
        if self._busy:
            await self._queue.put((text, message_id))
            logger.info(
                "OpenCode: queued message (queue_size=%d) text=%s",
                self._queue.qsize(),
                text[:60],
            )
            return {"ok": True, "queued": True, "queue_size": self._queue.qsize()}

        asyncio.create_task(self._process_message(text, message_id))
        return {"ok": True, "queued": False}

    def get_output(self, since: int = 0) -> dict:
        """Return output messages since the given byte offset."""
        # Find messages after `since` offset
        messages = []
        current_offset = 0
        for msg in self._output:
            msg_size = msg.get("_size", 100)
            if current_offset >= since:
                messages.append({k: v for k, v in msg.items() if not k.startswith("_")})
            current_offset += msg_size

        return {
            "ok": True,
            "messages": messages,
            "count": len(messages),
            "since": since,
            "next_offset": self._output_bytes,
        }

    def get_health(self) -> dict:
        """Return driver health status."""
        idle_seconds = time.monotonic() - self._last_activity
        return {
            "status": "ok",
            "agent_type": "opencode",
            "session_id": self._session_id,
            "busy": self._busy,
            "queue_size": self._queue.qsize(),
            "output_messages": len(self._output),
            "output_bytes": self._output_bytes,
            "last_error": self._last_error,
            "idle_seconds": round(idle_seconds, 1),
        }

    async def stop(self):
        """Stop any running process."""
        if self._current_proc and self._current_proc.returncode is None:
            self._current_proc.kill()
            await self._current_proc.wait()

    # ------------------------------------------------------------------
    # Internal: message processing
    # ------------------------------------------------------------------

    async def _process_message(self, text: str, message_id: str | None = None):
        """Process a single user message via opencode run subprocess."""
        self._busy = True
        self._last_activity = time.monotonic()

        # Record user message in output buffer
        self._append_output("user", text)

        try:
            await self._run_opencode(text)
        except Exception as e:
            logger.error("OpenCode process error: %s", e, exc_info=True)
            self._last_error = f"{type(e).__name__}: {e}"
            self._append_output(
                "assistant",
                f"⚠️ OpenCode error: {self._last_error}",
                content_type="text",
            )
        finally:
            self._busy = False
            self._last_activity = time.monotonic()

            # Process next queued message if any
            if not self._queue.empty():
                next_text, next_mid = await self._queue.get()
                logger.info("OpenCode: dequeuing next message, text=%s", next_text[:60])
                asyncio.create_task(self._process_message(next_text, next_mid))

    async def _run_opencode(self, prompt: str):
        """Spawn opencode run and parse stdout NDJSON events."""
        args = ["run", "--format", "json"]

        if self._session_id:
            args.extend(["--session", self._session_id])
        if self._model:
            args.extend(["--model", self._model])

        args.extend(["--dir", self._work_dir])
        args.append(prompt)

        logger.info(
            "OpenCode: launching %s %s (session=%s)",
            self._cmd,
            " ".join(args[:6]) + "...",
            self._session_id or "new",
        )

        env = {**os.environ}
        # Ensure opencode can find its config
        env.setdefault("HOME", str(Path.home()))

        proc = await asyncio.create_subprocess_exec(
            self._cmd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._work_dir,
            env=env,
        )
        self._current_proc = proc

        # Read stdout line by line (NDJSON events)
        collected_text = []
        assert proc.stdout is not None
        assert proc.stderr is not None

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.decode().strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("OpenCode: non-JSON stdout line: %s", line[:100])
                continue

            self._handle_event(event, collected_text)

        # Wait for process to finish
        stderr_bytes = await proc.stderr.read()
        await proc.wait()

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode().strip()
            logger.error(
                "OpenCode: process exited with code %d, stderr=%s",
                proc.returncode,
                stderr_text[:300],
                exc_info=True,
            )
            if stderr_text and not collected_text:
                self._last_error = stderr_text[:200]
                self._append_output(
                    "assistant", f"⚠️ OpenCode error: {stderr_text[:200]}"
                )
        elif not collected_text:
            logger.warning(
                "OpenCode: process exited successfully but produced no text output"
            )

        self._current_proc = None
        self._save_state()

    def _handle_event(self, event: dict, collected_text: list):
        """Handle a single NDJSON event from opencode run."""
        event_type = event.get("type", "")

        if event_type == "step_start":
            # Extract session ID
            part = event.get("part", {})
            sid = part.get("sessionID", "")
            if sid and sid != self._session_id:
                logger.info(
                    "OpenCode: session_id updated: %s → %s", self._session_id, sid
                )
                self._session_id = sid

        elif event_type == "text":
            part = event.get("part", {})
            text = part.get("text", "")
            if text:
                collected_text.append(text)
                self._append_output("assistant", text)

        elif event_type == "tool_use":
            part = event.get("part", {})
            tool_name = part.get("name", "unknown")
            tool_input = part.get("input", "")
            summary = f"🔧 Tool: {tool_name}"
            if isinstance(tool_input, str) and tool_input:
                summary += f"\n{tool_input[:200]}"
            elif isinstance(tool_input, dict):
                # Show key fields for common tools
                if "command" in tool_input:
                    summary += f"\n$ {tool_input['command'][:200]}"
                elif "file_path" in tool_input:
                    summary += f"\n📄 {tool_input['file_path']}"
            self._append_output("assistant", summary, content_type="tool_use")

        elif event_type == "tool_result":
            part = event.get("part", {})
            output = part.get("output", "")
            if output and isinstance(output, str) and len(output) > 0:
                # Truncate long tool results
                display = output[:500] + ("..." if len(output) > 500 else "")
                self._append_output("assistant", display, content_type="tool_result")

        elif event_type == "reasoning":
            part = event.get("part", {})
            text = part.get("text", "")
            if text:
                self._append_output("assistant", text, content_type="thinking")

        elif event_type == "error":
            error_msg = event.get("error", str(event))
            logger.error("OpenCode event error: %s", error_msg, exc_info=True)
            self._last_error = str(error_msg)[:200]

        elif event_type == "step_finish":
            part = event.get("part", {})
            cost = part.get("cost", 0)
            tokens = part.get("tokens", {})
            logger.info(
                "OpenCode: step_finish cost=%.4f tokens=%s",
                cost,
                json.dumps(tokens) if tokens else "?",
            )

    # ------------------------------------------------------------------
    # Output buffer
    # ------------------------------------------------------------------

    def _append_output(self, role: str, text: str, content_type: str = "text"):
        """Append a message to the output buffer."""
        msg = {
            "role": role,
            "text": text,
            "content_type": content_type,
            "_size": len(text.encode()) + 50,  # Approximate size for offset calc
        }
        self._output.append(msg)
        self._output_bytes += msg["_size"]

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self):
        """Persist session_id to disk for resume after restart."""
        state = {
            "session_id": self._session_id,
            "total_turns": len([m for m in self._output if m["role"] == "user"]),
        }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state, indent=2))
        except OSError as e:
            logger.error("Failed to save opencode state: %s", e, exc_info=True)

    def _load_state(self):
        """Load persisted session_id on startup."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                self._session_id = data.get("session_id", "")
                if self._session_id:
                    logger.info("OpenCode: restored session_id=%s", self._session_id)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load opencode state: %s", e)
