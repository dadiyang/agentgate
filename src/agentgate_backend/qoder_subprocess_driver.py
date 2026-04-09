"""Qoder CLI subprocess driver — implements AgentDriver protocol.

Manages Qoder CLI via `qodercli -p <prompt> --yolo --output-format stream-json`
subprocess calls.  No tmux required.  Each user message spawns a new process;
multi-turn continuity is maintained via `-r <session_id>`.

stream-json event format (observed from qodercli 0.1.x):
  {"type":"system",    "subtype":"init",    "session_id":"...", "done":false}
  {"type":"assistant", "subtype":"message", "message":{...},   "session_id":"...", "done":false}
  {"type":"result",    "subtype":"success", "message":{...},   "session_id":"...", "done":true}
  (result line appears twice — second is deduplicated)
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

_QODER_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("rate limit", "API rate limited"),
    ("unauthorized", "Auth error"),
    ("api error", "API error"),
    ("model not found", "Model not found"),
]


class QoderSubprocessDriver:
    """AgentDriver for Qoder CLI via per-turn subprocess mode."""

    def __init__(
        self,
        qoder_command: str = "qodercli",
        work_dir: str = "",
        model: str = "",
    ):
        self._command = qoder_command
        self._work_dir = work_dir
        self._model = model

        self._output: list[dict] = []
        self._output_bytes: int = 0

        self._session_id: str = ""
        self._state_file = config.instance_dir / "qoder_state.json"
        self._load_state()

        self._busy = False
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._current_proc: asyncio.subprocess.Process | None = None
        self._last_error: str | None = None
        self._last_activity: float = time.monotonic()

    # --- AgentDriver protocol ---

    def get_start_command(self, work_dir: str) -> str:
        return self._command

    def build_recovery_command(self, window_name: str) -> str:
        return self._command

    async def accept_startup_prompts(self, window_name: str) -> bool:
        return False

    async def inject(self, window_name: str, text: str) -> tuple[bool, str]:
        if self._busy:
            await self._queue.put(text)
            logger.info("Qoder subprocess: queued (size=%d)", self._queue.qsize())
            return True, "Queued"
        task = asyncio.create_task(self._process_message(text))
        task.add_done_callback(
            lambda t: t.cancelled() or (
                t.exception() and logger.error("Qoder process task failed: %s", t.exception())
            )
        )
        return True, "Injected"

    async def read_output(self, window_name: str, since: int) -> OutputResult:
        messages = []
        current_offset = 0
        for msg in self._output:
            msg_size = msg.get("_size", 100)
            if current_offset >= since:
                messages.append({k: v for k, v in msg.items() if not k.startswith("_")})
            current_offset += msg_size
        return OutputResult(messages=messages, count=len(messages), cursor=self._output_bytes)

    @property
    def process_name(self) -> str:
        return config.process_name

    @property
    def error_patterns(self) -> list[tuple[str, str]]:
        return _QODER_ERROR_PATTERNS

    def close(self):
        if self._current_proc and self._current_proc.returncode is None:
            self._current_proc.kill()

    # --- Internal: message processing ---

    async def _process_message(self, text: str):
        self._busy = True
        self._last_activity = time.monotonic()
        self._append_output("user", text)

        try:
            await asyncio.wait_for(self._run_qoder(text), timeout=300)
        except asyncio.TimeoutError:
            logger.error("Qoder: process timed out after 300s")
            if self._current_proc and self._current_proc.returncode is None:
                self._current_proc.kill()
                await self._current_proc.wait()
            self._append_output("assistant", "⚠️ Qoder error: timed out after 300s")
        except Exception as e:
            logger.error("Qoder process error: %s", e, exc_info=True)
            self._append_output("assistant", f"⚠️ Qoder error: {type(e).__name__}: {e}")
        finally:
            self._busy = False
            self._last_activity = time.monotonic()
            if not self._queue.empty():
                next_text = await self._queue.get()
                logger.info("Qoder: dequeuing next message")
                asyncio.create_task(self._process_message(next_text))

    async def _run_qoder(self, prompt: str):
        """Spawn qodercli -p and parse stream-json stdout."""
        args = [self._command, "-p", prompt, "--yolo", "--output-format", "stream-json"]
        if self._session_id:
            args.extend(["-r", self._session_id])
            logger.info("Qoder: resuming session %s", self._session_id)
        if self._model:
            args.extend(["--model", self._model])

        logger.info("Qoder: launching %s (session=%s)", " ".join(args[:5]) + "...", self._session_id or "new")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._work_dir or None,
            env={**os.environ, "HOME": str(Path.home())},
        )
        self._current_proc = proc

        seen_result = False
        assert proc.stdout is not None

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.decode().strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Qoder: JSON parse error: %s | line=%r", e, line[:200])
                continue

            done = self._handle_event(event, seen_result)
            if done:
                seen_result = True

        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            stderr_bytes = b""
            await proc.wait()

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode().strip()
            logger.error("Qoder: exited with code %d, stderr=%s", proc.returncode, stderr_text[:300])
            if stderr_text:
                self._append_output("assistant", f"⚠️ Qoder error: {stderr_text[:200]}")

        self._current_proc = None
        self._save_state()

    def _handle_event(self, event: dict, seen_result: bool) -> bool:
        """Parse one stream-json event. Returns True if this is the result (done) event."""
        etype = event.get("type", "")
        subtype = event.get("subtype", "")

        # Capture session_id from any event
        sid = event.get("session_id", "")
        if sid and sid != self._session_id:
            logger.info("Qoder: session_id %s → %s", self._session_id or "new", sid)
            self._session_id = sid

        if etype == "assistant" and subtype == "message":
            content = event.get("message", {}).get("content", [])
            for part in content:
                ptype = part.get("type", "")
                if ptype == "text":
                    text = part.get("text", "")
                    if text:
                        self._append_output("assistant", text)
                elif ptype == "tool_use":
                    name = part.get("name", "unknown")
                    inp = part.get("input", {})
                    summary = f"🔧 {name}"
                    if isinstance(inp, dict):
                        if "command" in inp:
                            summary += f"\n$ {inp['command'][:200]}"
                        elif "file_path" in inp:
                            summary += f"\n📄 {inp['file_path']}"
                    self._append_output("assistant", summary, content_type="tool_use")
                # skip "finish" type parts (end_turn marker, no text)

        elif etype == "result":
            if seen_result:
                # Qoder emits result twice — skip second
                return False
            if subtype != "success":
                error_msg = event.get("message", {}).get("content", [{}])[0].get("text", str(event))
                logger.error("Qoder: result subtype=%s: %s", subtype, error_msg[:200])
                self._append_output("assistant", f"⚠️ Qoder error: {error_msg[:200]}")
            return True  # signal done

        return False

    # --- Output buffer ---

    def _append_output(self, role: str, text: str, content_type: str = "text"):
        msg = {"role": role, "text": text, "content_type": content_type,
               "_size": len(text.encode()) + 50}
        self._output.append(msg)
        self._output_bytes += msg["_size"]
        if len(self._output) > _MAX_OUTPUT_ENTRIES:
            self._output = self._output[_MAX_OUTPUT_ENTRIES // 2:]

    # --- State persistence ---

    def _save_state(self):
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({"session_id": self._session_id}, indent=2))
        except OSError as e:
            logger.error("Qoder: failed to save state: %s", e)

    def _load_state(self):
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                self._session_id = data.get("session_id", "")
                if self._session_id:
                    logger.info("Qoder: restored session_id=%s", self._session_id)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Qoder: failed to load state: %s", e, exc_info=True)
