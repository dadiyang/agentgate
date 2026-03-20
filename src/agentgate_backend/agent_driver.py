"""AgentDriver protocol — unified abstraction for all agent backends.

Upper layers (inject_server, self_monitor, main) depend ONLY on this protocol.
They do not know whether the agent runs in tmux, subprocess, or any other mode.

Agent type (CC, OpenCode, Aider, ...) and runtime mode (tmux, subprocess, ...)
are both implementation details hidden inside each driver.

Each driver receives its dependencies (tmux_manager, session_manager, etc.)
at construction time — NOT through protocol method parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class OutputResult:
    """Result of an incremental output read.

    The `cursor` is opaque to the framework — each driver defines its own
    cursor semantics (byte offset for CC JSONL, timestamp for OpenCode SQLite,
    buffer offset for subprocess mode, etc.).
    """

    messages: list[dict] = field(default_factory=list)
    count: int = 0
    cursor: int = 0


@runtime_checkable
class AgentDriver(Protocol):
    """Unified protocol for all agent backends."""

    # --- Lifecycle ---

    def get_start_command(self, work_dir: str) -> str:
        """Return the command to start the agent."""
        ...

    def build_recovery_command(self, window_name: str) -> str:
        """Return the command to recover/resume after crash."""
        ...

    async def accept_startup_prompts(self, window_name: str) -> bool:
        """Handle interactive prompts after agent starts.

        Returns True if a prompt was handled, False otherwise.
        """
        ...

    # --- Message flow ---

    async def inject(self, window_name: str, text: str) -> tuple[bool, str]:
        """Inject a user message into the agent.

        Returns (success, status_message).
        """
        ...

    async def read_output(self, window_name: str, since: int) -> OutputResult:
        """Read incremental output since the given cursor.

        Cursor semantics are driver-defined and opaque to callers.
        """
        ...

    # --- Health ---

    @property
    def process_name(self) -> str:
        """Process name for liveness detection (e.g. 'claude', '.opencode')."""
        ...

    @property
    def error_patterns(self) -> list[tuple[str, str]]:
        """(pattern, label) pairs for error scanning."""
        ...
